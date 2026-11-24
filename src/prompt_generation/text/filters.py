"""Stage 2 filters: each check returns a drop reason, or None when the sentence passes."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from src.importers.framework import FieldError
from src.importers.prompts import SentenceRules
from src.prompt_generation.text.lexicon import tokenize

OPENING_QUOTES = "“\""
CLOSING_QUOTE_OPTIONAL = r"[”\"]?"
END_RE = re.compile(rf"[.?!]{CLOSING_QUOTE_OPTIONAL}$")
START_RE = re.compile(rf"^[{re.escape(OPENING_QUOTES)}]?([^\W\d_])")
LIST_MARKER_RE = re.compile(r"\(\s*(?:[ivxIVX]+|[a-zA-Z])\s*\)")
URL_OR_EMAIL_RE = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.-]+")
OCR_CHARS_RE = re.compile(r"[\]\[|{}\\~^]")
OCR_QUOTE_IN_WORD_RE = re.compile(r"\w[\"“”]\w")
MID_SENTENCE_BREAK_RE = re.compile(r"[.?!]\s+([^\W\d_]+)")
PUNCT_PAIR_RE = re.compile(r"[^\w\s]{2}")
# Adjacent punctuation pairs that are allowed in OCR sentences.
ALLOWED_PUNCT_PAIRS = {punct + quote for punct in ".,?!" for quote in '”"'}


# ========
# SETTINGS
# ========


@dataclass(frozen=True)
class FilterRules:
    importer: SentenceRules
    stopwords: frozenset[str]
    english_max_share: float
    ocr_max_unknown: int
    min_words: int
    max_words: int


def political_patterns(keywords: Iterable[str]) -> list[re.Pattern[str]]:
    """Whole-word matches. All-caps keywords are case-sensitive, so "PP" does not match "pp" in a word."""
    patterns = []
    for keyword in keywords:
        body = r"\s+".join(re.escape(part) for part in keyword.split())
        flags = 0 if keyword.isupper() else re.IGNORECASE
        patterns.append(re.compile(rf"(?<!\w){body}(?!\w)", flags))
    return patterns


def is_political(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


# ======
# CHECKS
# ======


def _starts_upper(text: str) -> bool:
    match = START_RE.match(text)
    return bool(match and match.group(1).isupper())


def _quotes_balanced(text: str) -> bool:
    depth = 0
    for char in text:
        if char == "“":
            depth += 1
        elif char == "”":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and text.count('"') % 2 == 0


def _english_share(text: str, stopwords: frozenset[str]) -> float:
    tokens = tokenize(text)
    if not tokens:
        return 0.0
    return sum(token in stopwords for token in tokens) / len(tokens)


def _ocr_noise(text: str) -> bool:
    if OCR_CHARS_RE.search(text) or OCR_QUOTE_IN_WORD_RE.search(text):
        return True
    if any(left.islower() and right.isupper() for left, right in zip(text, text[1:])):
        return True
    return any(pair.group() not in ALLOWED_PUNCT_PAIRS for pair in PUNCT_PAIR_RE.finditer(text))


def drop_reason(
    text: str, rules: FilterRules, *, ocr: bool, unknown: list[str], check_min_words: bool = True,
) -> str | None:
    """The first rule the sentence breaks, in the order the spec lists them; None if it passes."""
    words = len(text.split())
    if not _starts_upper(text):
        return "bad_start"
    if not END_RE.search(text):
        return "bad_end"
    if check_min_words and words < rules.min_words:
        return "too_short"
    if words > rules.max_words:
        return "too_long"
    if any(char.isdigit() for char in text):
        return "has_digit"
    if LIST_MARKER_RE.search(text) or ".." in text or URL_OR_EMAIL_RE.search(text):
        return "list_or_junk"
    if "(" in text or ")" in text:
        return "has_brackets"
    if not _quotes_balanced(text):
        return "unbalanced"
    if any(match.group(1)[0].islower() for match in MID_SENTENCE_BREAK_RE.finditer(text)):
        return "mid_sentence_break"
    if _english_share(text, rules.stopwords) > rules.english_max_share:
        return "english"
    if ocr and _ocr_noise(text):
        return "ocr_noise"
    if ocr and len(unknown) > rules.ocr_max_unknown:
        return "ocr_unknown_words"
    # Short component sentences can still form a valid unit; check importer length after grouping.
    try:
        rules.importer.clean(text)
    except FieldError as exc:
        if check_min_words or any(
            reason != f"fewer than {rules.importer.hard_min} words" for reason in exc.reasons
        ):
            return "importer_check"
    return None
