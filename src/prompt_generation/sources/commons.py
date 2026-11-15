"""Wikimedia Commons source (no account or key needed).

Uses the MediaWiki API: a full-text search of the File namespace as a generator, with
imageinfo + extmetadata for licence, author, description, categories and GPS position.
API etiquette: a descriptive User-Agent with contact details (set HARVEST_CONTACT_EMAIL),
one request at a time, and maxlag.
"""

from __future__ import annotations

import html
import re
from typing import Any

from ..models import Candidate

API_URL = "https://commons.wikimedia.org/w/api.php"
PAGE_SIZE = 50  # generator=search allows up to 50 per request for normal users

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: str | None) -> str:
    """Strip HTML from extmetadata values (they often contain links and spans)."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _meta(ext: dict[str, Any], key: str) -> str:
    return str(ext.get(key, {}).get("value", "") or "")


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class CommonsSource:
    name = "commons"

    def __init__(self, http, thumb_width: int = 512):
        self.http = http
        self.thumb_width = thumb_width

    # ======
    # SEARCH
    # ======

    def search(self, query: str, domain_key: str, limit: int) -> list[Candidate]:
        candidates: list[Candidate] = []
        offset = 0
        while len(candidates) < limit:
            params = self._params(query, offset, min(PAGE_SIZE, limit - len(candidates)))
            data = self.http.get_json(self.name, API_URL, params)
            if "error" in data:
                raise RuntimeError(f"Commons API error: {data['error']}")
            page_candidates = parse_response(data, query, domain_key)
            candidates.extend(page_candidates)
            next_offset = data.get("continue", {}).get("gsroffset")
            if next_offset is None or not page_candidates:
                break
            offset = int(next_offset)
        return candidates[:limit]

    def _params(self, query: str, offset: int, size: int) -> dict[str, Any]:
        return {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            # filetype:bitmap limits results to photos and other raster images
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6",
            "gsrlimit": str(size),
            "gsroffset": str(offset),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": str(self.thumb_width),
            "iiextmetadatafilter": (
                "LicenseShortName|LicenseUrl|Artist|ImageDescription|"
                "DateTimeOriginal|Categories|GPSLatitude|GPSLongitude|ObjectName"
            ),
            "maxlag": "5",
        }


# =======
# PARSING
# =======


def parse_response(data: dict[str, Any], query: str, domain_key: str) -> list[Candidate]:
    """Turn one API response into candidates, in search-rank order."""
    pages = data.get("query", {}).get("pages", [])
    if isinstance(pages, dict):  # formatversion=1 shape, just in case
        pages = list(pages.values())
    pages = sorted(pages, key=lambda p: p.get("index", 0))

    out: list[Candidate] = []
    for page in pages:
        info_list = page.get("imageinfo") or []
        if not info_list:
            continue
        info = info_list[0]
        ext = info.get("extmetadata", {})
        title = page.get("title", "")
        categories = [c.strip() for c in _meta(ext, "Categories").split("|") if c.strip()]

        out.append(
            Candidate(
                source="commons",
                source_id=str(page.get("pageid", title)),
                query=query,
                query_domain=domain_key,
                title=_clean(_meta(ext, "ObjectName")) or title.removeprefix("File:"),
                description=_clean(_meta(ext, "ImageDescription"))[:500],
                tags=categories,
                creator=_clean(_meta(ext, "Artist"))[:200],
                licence=_clean(_meta(ext, "LicenseShortName")),
                licence_url=_meta(ext, "LicenseUrl"),
                source_url=info.get("descriptionurl", ""),
                image_url=info.get("url", ""),
                thumb_url=info.get("thumburl", ""),
                mime_type=info.get("mime", ""),
                width=int(info.get("width", 0) or 0),
                height=int(info.get("height", 0) or 0),
                lat=_float(_meta(ext, "GPSLatitude")),
                lon=_float(_meta(ext, "GPSLongitude")),
                date_taken=_clean(_meta(ext, "DateTimeOriginal"))[:40],
            )
        )
    return out
