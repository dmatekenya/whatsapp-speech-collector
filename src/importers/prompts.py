import re
import unicodedata
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.config import PROJECT_ROOT
from src.importers.framework import (
    BatchImporter,
    FieldError,
    FieldSpec,
    ImportConfig,
    ImportReport,
    ParsedRow,
    load_config,
    next_id,
    parse_specs,
    normalize_whitespace,
    peek_next_id,
)
from src.storage.models import Prompt

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "prompts.yaml"

TRAILING_PUNCTUATION = '.,?!;:-"“” '
LINE_BREAKS = re.compile(r"[\r\n]+")
SENTENCE_END = ".?!"
# Replaced by the period when a line ends in one of these before a break.
WEAK_PUNCTUATION = ",;:-"
CLOSING_QUOTES = "\"”’'"
LETTER_RUNS = re.compile(r"[^\W\d_]+")


# ============================================================
# Text rules
# ============================================================
def normalize_domain(value: str, spec: FieldSpec | None = None) -> str:
    """Lowercase, trimmed, spaces to underscores: "Health Care " -> "health_care"."""
    return "_".join(value.lower().split())


class SentenceRules:
    def __init__(self, options: dict[str, Any]) -> None:
        self.hard_min, self.hard_max = (int(n) for n in options["hard_limit_words"])
        # Optional soft range; without it no length flags are set.
        target = options.get("target_words")
        self.target_min, self.target_max = (int(n) for n in target) if target else (None, None)
        if target and not self.hard_min <= self.target_min <= self.target_max <= self.hard_max:
            raise ValueError("prompts config: need hard_limit_words to contain target_words")
        self.allow_digits = bool(options.get("allow_digits", False))
        self.check_characters = bool(options.get("check_characters", True))
        self.apostrophes = str(options.get("apostrophes", "'"))
        self.allowed = set(str(options.get("allowed_punctuation", ""))) | set(self.apostrophes)
        self.allowed |= set(str(options.get("quotes", "")))
        self.all_caps_exceptions = {str(word) for word in options.get("all_caps_exceptions", [])}

    @staticmethod
    def has_line_breaks(value: str) -> bool:
        return LINE_BREAKS.search(value) is not None

    @staticmethod
    def join_lines(value: str) -> str:
        """Join lines into one: "A?\\n\\nB" -> "A? B", "A\\nB" -> "A. B", "A,\\nB" -> "A. B"."""
        joined = ""
        for line in (part.strip() for part in LINE_BREAKS.split(value)):
            if not line:
                continue
            if not joined:
                joined = line
            elif joined.rstrip(CLOSING_QUOTES)[-1:] in tuple(SENTENCE_END):
                joined = f"{joined} {line}"
            else:
                joined = f"{joined.rstrip(WEAK_PUNCTUATION).rstrip()}. {line}"
        return normalize_whitespace(joined)

    def all_caps_words(self, sentence: str) -> list[str]:
        """Words of 3+ letters written entirely in capitals, minus the exceptions."""
        found: list[str] = []
        for word in LETTER_RUNS.findall(sentence):
            if len(word) >= 3 and word.isupper() and word not in self.all_caps_exceptions:
                if word not in found:
                    found.append(word)
        return found

    def clean(self, value: str) -> str:
        """Validate a sentence; return it NFC-normalized with whitespace collapsed.

        Raises FieldError listing every rule the sentence breaks.
        """
        messages: list[str] = []
        reasons: list[str] = []

        def broken(message: str, reason: str | None = None) -> None:
            messages.append(message)
            reasons.append(reason or message)

        # Every other rule applies to the joined text.
        if self.has_line_breaks(value):
            value = self.join_lines(value)
        sentence = normalize_whitespace(unicodedata.normalize("NFC", value))
        if not self.allow_digits and any(ch.isdigit() for ch in sentence):
            broken("contains digits")
        bad = sorted(
            {
                ch
                for ch in sentence
                # Digits have their own rule (allow_digits).
                if not (ch.isalpha() or ch.isdigit() or ch == " " or ch in self.allowed)
                and not unicodedata.category(ch).startswith("M")  # combining accents
            }
        ) if self.check_characters else []
        if bad:
            broken("contains characters that aren't allowed: " + " ".join(repr(ch) for ch in bad))
        words = len(sentence.split())
        if words < self.hard_min:
            broken(f"has {words} word(s) (min {self.hard_min})", f"fewer than {self.hard_min} words")
        elif words > self.hard_max:
            broken(f"has {words} words (max {self.hard_max})", f"more than {self.hard_max} words")
        if messages:
            raise FieldError("; ".join(messages), reasons)
        return sentence

    def length_flag(self, sentence: str) -> str | None:
        """"short" or "long" outside target_words (a soft rule), else None.

        TODO(recordings linked to prompts): word count is only a proxy. Once
        recordings reference prompts, deactivate prompts whose median recording
        duration is over 30 s or under 5 s.
        """
        if self.target_min is None or self.target_max is None:
            return None
        words = len(sentence.split())
        if words < self.target_min:
            return "short"
        if words > self.target_max:
            return "long"
        return None

    def normalize(self, sentence: str) -> str:
        """Comparison key: lowercase, ' for all apostrophes, single spaces, no trailing punctuation."""
        normalized = unicodedata.normalize("NFC", sentence).lower()
        normalized = re.sub(f"[{re.escape(self.apostrophes)}]", "'", normalized)
        return normalize_whitespace(normalized).rstrip(TRAILING_PUNCTUATION)


# ============================================================
# Importer
# ============================================================
class PromptImporter(BatchImporter):
    title = "PROMPTS"
    id_column = "prompt_id"
    allows_partial = True  # invalid rows go to <name>_rejected.csv; --strict turns this off

    def __init__(self, config: ImportConfig, type_key: str, source_file: str) -> None:
        types = config.options["prompt_types"]
        if type_key not in types:
            raise ValueError(f"Unknown prompt type {type_key!r}; configured: {', '.join(types)}")
        type_options = types[type_key]
        # Each prompt type defines its own columns.
        super().__init__(
            ImportConfig(
                fields=parse_specs(type_options.get("fields")),
                attributes=parse_specs(type_options.get("attributes")),
                options=config.options,
            )
        )
        self.type_options = type_options
        self.prompt_type = self.type_options["prompt_type"]
        self.title = f"PROMPTS ({type_key})"
        self.source_file = source_file
        self.rules = SentenceRules(self.type_options)
        self.parsers["sentence"] = lambda value, spec: self.rules.clean(value)
        self.parsers["domain"] = normalize_domain

    @classmethod
    def from_file(
        cls, type_key: str, csv_path: Path, config_path: Path = DEFAULT_CONFIG
    ) -> "PromptImporter":
        return cls(load_config(config_path), type_key, source_file=csv_path.name)

    def _key(self, row: ParsedRow) -> str:
        return self.rules.normalize(row.values["text"])

    def validate_file(self, rows: list[ParsedRow], report: ImportReport) -> None:
        first_row: dict[str, int] = {}
        for row in rows:
            if "text" not in row.values:
                continue  # already reported as invalid
            key = self._key(row)
            if not key:
                report.error(row.number, "text", "is empty after normalization")
            elif key in first_row:
                row.skip = True
                report.warn(row.number, "text", f"duplicate of row {first_row[key]}; skipped")
            else:
                first_row[key] = row.number

    def lock(self, session: Session) -> None:
        session.execute(text("LOCK TABLE prompts IN SHARE ROW EXCLUSIVE MODE"))

    def _flags(self, row: ParsedRow, report: ImportReport) -> dict[str, Any]:
        """Soft-rule flags for a prompt about to be imported; each one is a warning."""
        flags: dict[str, Any] = {}
        sentence = row.values["text"]
        # The parsed text is already joined; the raw cell shows whether it was.
        if self.rules.has_line_breaks(row.raw.get("text", "")):
            flags["joined_lines"] = True
            preview = sentence if len(sentence) <= 100 else sentence[:97] + "..."
            report.warn(
                row.number,
                "text",
                f"line breaks joined into one line; imported with flag joined_lines (now {preview!r})",
            )
        caps = self.rules.all_caps_words(sentence)
        if caps:
            flags["all_caps"] = True
            report.warn(
                row.number,
                "text",
                f"all-caps word(s) {', '.join(caps)}; imported with flag all_caps",
            )
        length = self.rules.length_flag(sentence)
        if length is not None:
            flags["length"] = length
            words = len(row.values["text"].split())
            report.warn(
                row.number,
                "text",
                f"{words} words, outside the target {self.rules.target_min}-"
                f"{self.rules.target_max}; imported with flag length={length}",
            )
        for name, value in flags.items():
            report.counts[f"flagged {name}" if value is True else f"flagged {name}={value}"] += 1
        return flags

    def apply(
        self, session: Session, rows: list[ParsedRow], report: ImportReport, dry_run: bool
    ) -> dict[int, str]:
        keys = [self._key(row) for row in rows]
        existing = {
            p.normalized_text: p
            for p in session.scalars(
                select(Prompt).where(
                    Prompt.prompt_type == self.prompt_type, Prompt.normalized_text.in_(keys)
                )
            )
        }
        given_ids = [row.values["prompt_id"] for row in rows if "prompt_id" in row.values]
        by_id = {
            p.prompt_id: p
            for p in session.scalars(select(Prompt).where(Prompt.prompt_id.in_(given_ids)))
        }

        ids: dict[int, str] = {}
        new_rows: list[ParsedRow] = []
        key_ids: dict[str, str] = {}
        for row, key in zip(rows, keys, strict=True):
            if row.skip:
                report.counts["skipped: duplicate in file"] += 1
                continue
            if key in existing:
                report.counts["skipped: already in prompt bank"] += 1
                ids[row.number] = key_ids[key] = existing[key].prompt_id
                continue
            given_id = row.values.get("prompt_id")
            if given_id in by_id:
                # TODO(conversation engine): if the prompt has never been
                # assigned, update its text in place instead. Assignments don't
                # exist yet, so changed text becomes a new prompt.
                report.warn(
                    row.number,
                    "text",
                    f"differs from {given_id}; imported as a new prompt (old one kept)",
                )
            new_rows.append(row)

        report.counts["would import" if dry_run else "imported"] += len(new_rows)
        flags_by_row = {row.number: self._flags(row, report) for row in new_rows}
        prefix, width, sequence = (
            self.type_options["id_prefix"],
            int(self.type_options["id_width"]),
            self.type_options["sequence"],
        )
        if dry_run:
            if new_rows:
                first = peek_next_id(session, sequence, prefix, width)
                report.notes.append(f"would assign {len(new_rows)} new ID(s), starting at {first}")
            return ids

        for row in new_rows:
            key = self._key(row)
            prompt_id = next_id(session, sequence, prefix, width)
            session.add(
                Prompt(
                    prompt_id=prompt_id,
                    prompt_type=self.prompt_type,
                    text=row.values["text"],
                    normalized_text=key,
                    source=row.values["source"],
                    domain=row.values.get("domain"),
                    attributes=row.attributes_with_extra(),
                    flags=flags_by_row[row.number],
                    source_file=self.source_file,
                )
            )
            ids[row.number] = key_ids[key] = prompt_id
        session.flush()

        # In-file duplicates get the ID of the row they duplicate.
        for row, key in zip(rows, keys, strict=True):
            if row.skip and key in key_ids:
                ids[row.number] = key_ids[key]
        return ids
