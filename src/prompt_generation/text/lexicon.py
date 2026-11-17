"""Word lexicon from clean text, used to check OCR sentences for unknown words."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

# Single-quote characters that are written as an apostrophe. Mapped before tokenising.
APOSTROPHE_VARIANTS = "‘’ʼʻ′"
_VARIANT_RE = re.compile(f"[{re.escape(APOSTROPHE_VARIANTS)}]")
_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")


# ======
# TOKENS
# ======


def tokenize(text: str) -> list[str]:
    """Lowercase words of letters, keeping inner apostrophes (ng'ombe is one word)."""
    return _WORD_RE.findall(_VARIANT_RE.sub("'", text).lower())


def unknown_words(text: str, lexicon: frozenset[str]) -> list[str]:
    return [word for word in tokenize(text) if word not in lexicon]


# =======
# LEXICON
# =======


def build_lexicon(texts: Iterable[str], min_count: int) -> frozenset[str]:
    """Words that occur at least min_count times across the clean texts."""
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(tokenize(text))
    return frozenset(word for word, count in counts.items() if count >= min_count)
