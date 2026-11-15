"""Metadata filters, applied before any image is downloaded."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import HarvestConfig
from .models import Candidate

# ========
# LICENCES
# ========

_OPEN_MARKERS = (
    "cc0", "cc by", "cc-by", "creative commons", "public domain", "pdm",
    "no known copyright", "no restrictions", "copyrighted free use",
    "attribution", "gfdl", "free art", "us government work",
)
_NC_PATTERN = re.compile(r"(\bnc\b|non-?commercial)")
_CLOSED_MARKERS = ("all rights reserved", "fair use", "non-free")


def licence_allowed(licence: str, licence_url: str, allow_non_commercial: bool) -> tuple[bool, str]:
    """Return (allowed, reason). Only openly licensed or public-domain images pass."""
    text = f"{licence} {licence_url}".lower().replace("_", " ")
    if not text.strip():
        return False, "licence_missing"
    if any(m in text for m in _CLOSED_MARKERS):
        return False, "licence_not_open"
    if _NC_PATTERN.search(text.replace("-", " ")) or "by-nc" in text:
        if not allow_non_commercial:
            return False, "licence_non_commercial"
    if any(m in text for m in _OPEN_MARKERS) or "creativecommons.org" in text:
        return True, ""
    return False, "licence_unrecognised"


# ================
# MALAWI RELEVANCE
# ================


def _contains_any(text: str, words: Iterable[str]) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def malawi_signal(c: Candidate, cfg: HarvestConfig) -> str:
    """Strongest evidence that the image is from Malawi: geo > category > text > none.

    The search query is deliberately ignored: every query mentions Malawi, so a match in
    the query proves nothing about the image itself.
    """
    if cfg.malawi_bbox.contains(c.lat, c.lon):
        return "geo"
    if _contains_any(" | ".join(c.tags).lower(), cfg.malawi_place_names):
        return "category"
    if _contains_any(f"{c.title} {c.description}".lower(), cfg.malawi_place_names):
        return "text"
    return "none"


# =============
# APPLY FILTERS
# =============


def apply_metadata_filters(candidates: list[Candidate], cfg: HarvestConfig) -> None:
    """Set malawi_signal and drop candidates that fail a metadata rule (in place)."""
    for c in candidates:
        c.malawi_signal = malawi_signal(c, cfg)

        ok, reason = licence_allowed(c.licence, c.licence_url, cfg.allow_non_commercial)
        if not ok:
            c.drop(reason)
            continue
        if c.mime_type and c.mime_type not in cfg.allowed_mime_types:
            c.drop(f"mime:{c.mime_type}")
            continue
        if _contains_any(f"{c.title} {' '.join(c.tags)}".lower(), cfg.unwanted_keywords):
            c.drop("unwanted_keyword")
            continue
        if c.width and c.height and (c.width < cfg.min_width or c.height < cfg.min_height):
            c.drop("too_small")
            continue
        if c.malawi_signal == "none":
            c.drop("no_malawi_signal")


def drop_exact_duplicates(candidates: list[Candidate]) -> None:
    """Drop repeats of the same source image or the same file URL (keeps the first)."""
    seen_ids: dict[str, str] = {}
    seen_urls: dict[str, str] = {}
    for c in candidates:
        if c.status == "dropped":
            continue
        cid = c.candidate_id
        if cid in seen_ids:
            c.drop(f"duplicate_of:{seen_ids[cid]}")
            continue
        if c.image_url and c.image_url in seen_urls:
            c.drop(f"duplicate_of:{seen_urls[c.image_url]}")
            continue
        seen_ids[cid] = cid
        if c.image_url:
            seen_urls[c.image_url] = cid
