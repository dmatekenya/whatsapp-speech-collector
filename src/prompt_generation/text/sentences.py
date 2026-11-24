"""Stage 2: prepare paragraph, group and sentence units, filter, dedupe and write CSVs."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.config import PROJECT_ROOT
from src.importers.prompts import SentenceRules
from src.prompt_generation.text.filters import FilterRules, drop_reason, is_political, political_patterns
from src.prompt_generation.text.lexicon import APOSTROPHE_VARIANTS, build_lexicon, unknown_words

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "sentence-extraction.yaml"
QUOTE_VARIANTS = {"„": "“", "‟": "”", "″": "”"}
BOUNDARY_RE = re.compile(r"([.?!]+)([”\"]?)(\s+)")
NEXT_SENTENCE_START_RE = re.compile(r"[“\"]?[^\W\d_]")
ABBREVIATION_LEAD = "(“‘'[\""

ALL_COLUMNS = [
    "sentence_id", "text", "word_count", "unit_type", "n_sentences", "block_index", "political", "source_category",
    "source_reference", "method", "source_file", "doc_id", "doc_title", "doc_date", "date_source",
    "page", "sentence_index", "unknown_count", "unknown_words", "status", "drop_reason",
]
# The importer reads text, source and domain. The rest are provenance and stay in the file.
KEPT_COLUMNS = [
    "text", "source", "domain", "domain_confidence", "domain_source", "sentence_id", "word_count",
    "unit_type", "n_sentences", "block_index", "political", "source_reference", "method", "source_file", "doc_id", "doc_title",
    "doc_date", "date_source", "page", "sentence_index",
]
DOMAIN_COLUMNS = ["domain", "domain_confidence", "domain_source"]
SCORE_COLUMNS = ["sentence_id", *DOMAIN_COLUMNS, "model", "scored_at"]


# ========
# SETTINGS
# ========


@dataclass(frozen=True)
class SentenceSettings:
    filters: FilterRules
    group_max_words: int
    apostrophe: str
    abbreviations: frozenset[str]
    political_keywords: tuple[str, ...]
    lexicon_min_count: int
    extracted_text_root: Path
    sentences_root: Path
    existing_sentences: Path

    @property
    def importer(self) -> SentenceRules:
        return self.filters.importer


def load_sentence_settings(path: Path = DEFAULT_CONFIG) -> SentenceSettings:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    prompts_raw = yaml.safe_load((PROJECT_ROOT / raw["prompts_config"]).read_text(encoding="utf-8"))
    importer = SentenceRules(prompts_raw["prompt_types"][raw["prompt_type"]])
    min_words, max_words = int(raw["min_words"]), int(raw["max_words"])
    group_max_words = int(raw["group_max_words"])
    if not 1 <= min_words <= group_max_words <= max_words:
        raise ValueError("sentence config: need 1 <= min_words <= group_max_words <= max_words")
    if (importer.hard_min, importer.hard_max) != (min_words, max_words) or (
        importer.target_min, importer.target_max
    ) != (min_words, max_words):
        raise ValueError("sentence word range must match both importer ranges")
    return SentenceSettings(
        filters=FilterRules(
            importer=importer,
            stopwords=frozenset(str(word).lower() for word in raw["english_stopwords"]),
            english_max_share=float(raw["english_max_share"]),
            ocr_max_unknown=int(raw["ocr_max_unknown_words"]),
            min_words=min_words,
            max_words=max_words,
        ),
        group_max_words=group_max_words,
        apostrophe=str(raw["apostrophe"]),
        abbreviations=frozenset(str(item) for item in raw["abbreviations"]),
        political_keywords=tuple(str(item) for item in raw["political_keywords"]),
        lexicon_min_count=int(raw["lexicon_min_count"]),
        extracted_text_root=PROJECT_ROOT / raw["extracted_text_root"],
        sentences_root=PROJECT_ROOT / raw["sentences_root"],
        existing_sentences=PROJECT_ROOT / raw["existing_sentences"],
    )


# ===========
# NORMALISING
# ===========


def normalise_text(text: str, apostrophe: str) -> str:
    """One apostrophe form, one double-quote form, single spaces."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(rf"(?<=[^\W\d_])['\"“”{APOSTROPHE_VARIANTS}]{{2,}}(?=[^\W\d_])", "'", text)
    for variant in APOSTROPHE_VARIANTS:
        text = text.replace(variant, apostrophe)
    for old, new in QUOTE_VARIANTS.items():
        text = text.replace(old, new)
    return re.sub(r'\.{3,}([”"]?)$', r".\1", " ".join(text.split()))


def clean_unit(text: str, apostrophe: str) -> str:
    """Repair only unmatched boundary quotes; internal imbalance is left for the filter."""
    text = normalise_text(text, apostrophe)
    if text.startswith("“") and text.count("“") > text.count("”"):
        text = text[1:].lstrip()
    if text.endswith("”") and text.count("”") > text.count("“"):
        text = text[:-1].rstrip()
    if text.count('"') % 2:
        if text.startswith('"'):
            text = text[1:].lstrip()
        elif text.endswith('"'):
            text = text[:-1].rstrip()
    return text


def dedupe_key(text: str) -> str:
    """Lowercase, apostrophes and quotes unified, punctuation removed (apostrophes kept)."""
    value = normalise_text(text, "'").lower()
    value = re.sub(r"[^\w\s']", " ", value)
    return " ".join(value.split())


def sentence_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# =========
# SPLITTING
# =========


def _ends_with_abbreviation(before: str, abbreviations: frozenset[str]) -> bool:
    tokens = before.split()
    if not tokens:
        return False
    token = tokens[-1].lstrip(ABBREVIATION_LEAD)
    if token in abbreviations:
        return True
    return len(token) == 1 and token.isalpha() and token.isupper()


def split_sentences(text: str, abbreviations: frozenset[str]) -> list[str]:
    """Split after . ? ! (and an optional closing quote) before an uppercase letter or opening quote.

    No split after an abbreviation, or after a single capital and "." (an initial).
    """
    sentences: list[str] = []
    start = 0
    for match in BOUNDARY_RE.finditer(text):
        following = NEXT_SENTENCE_START_RE.match(text, match.end())
        if not following or not following.group()[-1].isupper():
            continue
        if match.group(1) == "." and _ends_with_abbreviation(text[start : match.start()], abbreviations):
            continue
        sentences.append(text[start : match.start(3)].strip())
        start = match.end(3)
    rest = text[start:].strip()
    if rest:
        sentences.append(rest)
    return sentences


def _unit(texts: list[str], unit_type: str | None = None, reason: str | None = None) -> dict[str, Any]:
    return {
        "text": " ".join(texts),
        "unit_type": unit_type or ("group" if len(texts) > 1 else "sentence"),
        "n_sentences": len(texts),
        "reason": reason,
    }


def _group_sentences(texts: list[str], settings: SentenceSettings) -> list[dict[str, Any]]:
    groups: list[list[str]] = []
    for text in texts:
        if groups and len(" ".join([*groups[-1], text]).split()) <= settings.group_max_words:
            groups[-1].append(text)
        else:
            groups.append([text])
    if len(groups) > 1 and len(" ".join(groups[-1]).split()) < settings.filters.min_words:
        if len(" ".join(groups[-2] + groups[-1]).split()) <= settings.filters.max_words:
            groups[-2].extend(groups.pop())
    return [_unit(group) for group in groups]


def _block_units(
    block: str, settings: SentenceSettings, *, ocr: bool, lexicon: frozenset[str],
) -> list[dict[str, Any]]:
    text = clean_unit(block, settings.apostrophe)
    sentences = split_sentences(text, settings.abbreviations)
    if len(text.split()) <= settings.filters.max_words:
        return [_unit(sentences, unit_type="paragraph")]

    units: list[dict[str, Any]] = []
    segment: list[str] = []
    for sentence in sentences:
        sentence = clean_unit(sentence, settings.apostrophe)
        reason = drop_reason(
            sentence, settings.filters, ocr=ocr, unknown=unknown_words(sentence, lexicon),
            check_min_words=False,
        )
        if reason:
            units.extend(_group_sentences(segment, settings))
            segment = []
            units.append(_unit([sentence], reason=reason))
        else:
            segment.append(sentence)
    units.extend(_group_sentences(segment, settings))
    return units


def candidates(
    documents: list[dict[str, Any]], settings: SentenceSettings, lexicon: frozenset[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for doc in documents:
        index = 0
        for block_index, block in enumerate(doc["blocks"]):
            for unit in _block_units(block, settings, ocr=doc["method"] == "ocr", lexicon=lexicon):
                rows.append({**unit, "doc": doc, "sentence_index": index + 1, "block_index": block_index})
                index += unit["n_sentences"]
    return rows


# ========
# PIPELINE
# ========


def read_documents(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"No documents.jsonl at {path}; run the extract command first.")
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_bank(path: Path, apostrophe: str) -> list[str]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [normalise_text(row["text"], apostrophe) for row in csv.DictReader(handle) if row.get("text")]


def _row(
    candidate: dict[str, Any], *, sid: str, unknown: list[str],
    political: bool, reason: str | None,
) -> dict[str, Any]:
    doc = candidate["doc"]
    text = candidate["text"]
    return {
        "sentence_id": sid,
        "text": text,
        "word_count": len(text.split()),
        "unit_type": candidate["unit_type"],
        "n_sentences": candidate["n_sentences"],
        "block_index": candidate["block_index"],
        "political": political,
        "source_category": doc["source_category"],
        "source_reference": doc["source_reference"],
        "method": doc["method"],
        "source_file": doc["source_file"],
        "doc_id": doc["doc_id"],
        "doc_title": doc.get("doc_title") or "",
        "doc_date": doc.get("doc_date") or "",
        "date_source": doc.get("date_source") or "",
        "page": "" if doc.get("page") is None else doc["page"],
        "sentence_index": candidate["sentence_index"],
        "unknown_count": len(unknown),
        "unknown_words": "|".join(unknown),
        "status": "kept" if reason is None else "dropped",
        "drop_reason": reason or "",
    }


def run_sentences(*, run_id: str, settings: SentenceSettings) -> dict[str, Any]:
    """Filter, dedupe and write sentences-all.csv, sentences-kept.csv and run-log.json for one run."""
    documents = read_documents(settings.extracted_text_root / run_id / "documents.jsonl")
    bank = read_bank(settings.existing_sentences, settings.apostrophe)
    clean_texts = [
        normalise_text(block, settings.apostrophe)
        for doc in documents if doc["method"] != "ocr" for block in doc["blocks"]
    ] + bank
    lexicon = build_lexicon(clean_texts, settings.lexicon_min_count)
    rows = candidates(documents, settings, lexicon)
    bank_keys = {dedupe_key(text) for text in bank}
    patterns = political_patterns(settings.political_keywords)

    first_kept: dict[str, str] = {}
    output: list[dict[str, Any]] = []
    for candidate in rows:
        text = candidate["text"]
        ocr = candidate["doc"]["method"] == "ocr"
        unknown = unknown_words(text, lexicon)
        reason = candidate["reason"] or drop_reason(text, settings.filters, ocr=ocr, unknown=unknown)
        key = dedupe_key(text)
        sid = sentence_id(key)
        if reason is None:
            if key in bank_keys:
                reason = "already_in_bank"
            elif key in first_kept:
                reason = f"duplicate_of:{first_kept[key]}"
            else:
                first_kept[key] = sid
        output.append(
            _row(
                candidate, sid=sid, unknown=unknown,
                political=is_political(text, patterns), reason=reason,
            )
        )

    out_dir = settings.sentences_root / run_id
    kept = [row for row in output if row["status"] == "kept"]
    run_log = _run_log(run_id, documents, output, lexicon_size=len(lexicon), bank_size=len(bank))
    _write_outputs(out_dir, output, kept, run_log)
    return {**run_log, "output_dir": str(out_dir)}


def _group_counts(output: list[dict[str, Any]], field: str) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = {}
    for row in output:
        group = groups.setdefault(str(row[field]), {"found": 0, "kept": 0})
        group["found"] += 1
        group["kept"] += row["status"] == "kept"
    return groups


def _run_log(run_id: str, documents: list[dict[str, Any]], output: list[dict[str, Any]], *,
             lexicon_size: int, bank_size: int) -> dict[str, Any]:
    kept = sum(row["status"] == "kept" for row in output)
    return {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "documents": len(documents),
        "sentences_found": len(output),
        "sentences_kept": kept,
        "by_source_category": _group_counts(output, "source_category"),
        "by_method": _group_counts(output, "method"),
        "drop_reasons": dict(Counter(_reason_group(r["drop_reason"]) for r in output if r["status"] == "dropped")),
        "lexicon_words": lexicon_size,
        "bank_sentences": bank_size,
    }


def _reason_group(reason: str) -> str:
    return "duplicate" if reason.startswith("duplicate_of:") else reason


def _write_outputs(out_dir: Path, output: list[dict[str, Any]], kept: list[dict[str, Any]], run_log: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "sentences-all.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALL_COLUMNS)
        writer.writeheader()
        writer.writerows(output)

    scores = read_domain_scores(out_dir)
    write_domain_scores(out_dir / "domain-scores.csv", scores)
    kept_rows = [{**row, "source": row["source_category"]} for row in kept]
    merge_domain_scores(kept_rows, scores)
    write_kept(out_dir / "sentences-kept.csv", kept_rows)
    (out_dir / "run-log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_kept(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write the kept CSV through a temporary file, so an interrupted write never leaves a half file."""
    _write_csv(path, rows, KEPT_COLUMNS)


def read_domain_scores(run_dir: Path) -> dict[str, dict[str, str]]:
    """Read durable scores and carry forward legacy domains stored only in the kept CSV."""
    scores: dict[str, dict[str, str]] = {}
    path = run_dir / "domain-scores.csv"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                scores[row["sentence_id"]] = dict(row)
    legacy = run_dir / "sentences-kept.csv"
    if legacy.exists():
        with legacy.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("domain"):
                    scores.setdefault(row["sentence_id"], {column: row.get(column, "") for column in SCORE_COLUMNS})
    return scores


def merge_domain_scores(rows: list[dict[str, Any]], scores: dict[str, dict[str, str]]) -> None:
    for row in rows:
        score = scores.get(row["sentence_id"], {})
        row.update({column: score.get(column, "") for column in DOMAIN_COLUMNS})


def write_domain_scores(path: Path, scores: dict[str, dict[str, str]]) -> None:
    _write_csv(path, list(scores.values()), SCORE_COLUMNS)


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
