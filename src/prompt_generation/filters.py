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
    "ogl v", "open government licen", "open-government-licen",
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
_YEAR_RE = re.compile(r"\b(18|19|20)\d{2}\b")


def year_taken(c: Candidate) -> int | None:
    """First plausible year in the photo's date field, or None if there is none."""
    match = _YEAR_RE.search(c.date_taken or "")
    return int(match.group()) if match else None


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
        year = year_taken(c)
        if cfg.min_year and year is not None and year < cfg.min_year:
            c.drop("too_old")
            continue
        if c.query_domain == "unassigned":
            if c.malawi_signal != "geo":
                c.drop("no_malawi_signal")
            continue
        if c.malawi_signal == "none" and cfg.domain(c.query_domain).location == "malawi":
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


# ==============
# DIVERSITY CAPS
# ==============


def cap_per_creator_day(candidates: list[Candidate], max_n: int) -> None:
    """Keep the first max_n photos per source, creator and date, in search order."""
    if max_n <= 0:
        return
    counts: dict[tuple[str, str, str], int] = {}
    for c in candidates:
        if c.status == "dropped" or not c.creator or len(c.date_taken) < 10:
            continue
        key = (c.source, c.creator.lower(), c.date_taken[:10])
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > max_n:
            c.drop("event_cap")


def cap_per_creator_domain(candidates: list[Candidate], max_n: int) -> None:
    """Keep the first max_n photos per source, creator and query domain."""
    if max_n <= 0:
        return
    counts: dict[tuple[str, str, str], int] = {}
    for c in candidates:
        if c.status == "dropped" or not c.creator:
            continue
        key = (c.source, c.creator.lower(), c.query_domain)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > max_n:
            c.drop("creator_cap")
