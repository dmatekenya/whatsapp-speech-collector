"""Split newspaper page text into body blocks and drop headers, bylines, captions and headlines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ======
# MODELS
# ======


@dataclass(frozen=True)
class BlockRules:
    header_patterns: tuple[str, ...]
    byline_prefixes: tuple[str, ...] = ("Wolemba", "Ndemanga")
    byline_ndi_max_words: int = 4
    photo_credit: str = "Wojambula"
    min_headline_words: int = 15
    drop_cap_repair: bool = True
    ocr_max_line_chars: int = 70


@dataclass(frozen=True)
class DroppedBlock:
    index: int
    reason: str
    text: str


@dataclass
class BlockResult:
    kept: list[str] = field(default_factory=list)
    dropped: list[DroppedBlock] = field(default_factory=list)


# ======
# CHECKS
# ======

TERMINAL = re.compile(r'[.?!]["”’]?$')
CAPTION = re.compile(r"^(?:[A-Z][A-Z'’]*\s+)+[A-Z][A-Z'’]*\s*[—–-]")
DROP_CAP = re.compile(r"[A-Z]")


def split_blocks(text: str) -> list[str]:
    """Split on blank lines and join each block's lines with single spaces."""
    blocks = []
    for chunk in re.split(r"\n\s*\n", text):
        block = " ".join(chunk.split())
        if block:
            blocks.append(block)
    return blocks


def _drop_reason(block: str, rules: BlockRules) -> str | None:
    if any(re.search(pattern, block) for pattern in rules.header_patterns):
        return "header"
    if block.startswith(rules.byline_prefixes):
        return "byline"
    if block.startswith("Ndi ") and len(block.split()) - 1 <= rules.byline_ndi_max_words:
        return "byline"
    if rules.photo_credit in block:
        return "photo_credit"
    if CAPTION.match(block):
        return "caption"
    return None


def _repair_drop_caps(items: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Attach a lone capital letter block to the block that follows it."""
    out = []
    i = 0
    while i < len(items):
        index, text = items[i]
        if DROP_CAP.fullmatch(text) and i + 1 < len(items):
            next_index, next_text = items[i + 1]
            out.append((next_index, text + next_text))
            i += 2
        else:
            out.append((index, text))
            i += 1
    return out


def _join_columns(items: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Merge a block that does not end a sentence with a following block that starts lowercase."""
    out: list[tuple[int, str]] = []
    for index, text in items:
        if out and not TERMINAL.search(out[-1][1]) and text[:1].islower():
            prev_index, prev_text = out[-1]
            out[-1] = (prev_index, prev_text + " " + text)
        else:
            out.append((index, text))
    return out


def clean_blocks(page_text: str, rules: BlockRules, *, method: str) -> BlockResult:
    """Return kept body blocks and a record of every dropped block with its reason."""
    result = BlockResult()
    items: list[tuple[int, str]] = []
    chunks = [chunk for chunk in re.split(r"\n\s*\n", page_text) if chunk.strip()]
    for index, chunk in enumerate(chunks):
        block = " ".join(chunk.split())
        if method == "ocr" and any(len(line) > rules.ocr_max_line_chars for line in chunk.splitlines()):
            result.dropped.append(DroppedBlock(index, "merged_columns", block))
            continue
        if method == "ocr":
            block = re.sub(r"\blye\b", "Iye", block)
            block = re.sub(r"[_=§]+", " ", block)
            block = " ".join(block.split())
        reason = _drop_reason(block, rules)
        if reason:
            result.dropped.append(DroppedBlock(index, reason, block))
        else:
            items.append((index, block))

    if method == "text-layer" and rules.drop_cap_repair:
        items = _repair_drop_caps(items)
    items = _join_columns(items)

    for index, block in items:
        if len(block.split()) < rules.min_headline_words and not TERMINAL.search(block):
            result.dropped.append(DroppedBlock(index, "headline", block))
        else:
            result.kept.append(block)

    result.dropped.sort(key=lambda d: d.index)
    return result
