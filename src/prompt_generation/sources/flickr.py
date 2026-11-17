"""Flickr candidates: a once-per-run Malawi geo sweep and per-query text searches.

Search parameters: https://www.flickr.com/services/api/flickr.photos.search.html
Licence IDs/names come from flickr.photos.licenses.getInfo, cached by the shared client.
"""

from __future__ import annotations

import html
import os
import re
import time
from typing import Any

from ..config import HarvestConfig
from ..filters import licence_allowed
from ..models import Candidate

API_URL = "https://www.flickr.com/services/rest/"
PAGE_SIZE = 250
SEARCH_LIMIT = 4000
EXTRAS = "description,license,date_upload,date_taken,owner_name,original_format,geo,tags,o_dims,url_o,url_l,url_z"


class FlickrSource:
    name = "flickr"

    def __init__(self, http, cfg: HarvestConfig):
        self.http = http
        self.cfg = cfg
        self.api_key = os.environ.get(cfg.flickr.api_key_env, "").strip()
        if not self.api_key:
            raise RuntimeError(f"{cfg.flickr.api_key_env} is not set")
        self.licences: dict[str, tuple[str, str]] | None = None

    @property
    def text_enabled(self) -> bool:
        return "text" in self.cfg.flickr.modes

    # ======
    # SEARCH
    # ======

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        data = self.http.get_json(self.name, API_URL, {
            "api_key": self.api_key, "format": "json", "nojsoncallback": 1, **params,
        })
        if data.get("stat") == "fail":
            raise RuntimeError(f"Flickr API error {data.get('code')}: {data.get('message')}")
        return data

    def _base_params(self) -> dict[str, Any]:
        if self.licences is None:
            data = self._get({"method": "flickr.photos.licenses.getInfo"})
            self.licences = {
                str(item["id"]): (item["name"], item.get("url", ""))
                for item in data.get("licenses", {}).get("license", [])
                if licence_allowed(item["name"], item.get("url", ""),
                                   self.cfg.allow_non_commercial)[0]
            }
        if not self.licences:
            raise RuntimeError("Flickr returned no allowed licence IDs")
        return {"method": "flickr.photos.search", "license": ",".join(self.licences),
                "media": "photos", "content_type": 1, "sort": "date-posted-desc",
                "per_page": PAGE_SIZE, "extras": EXTRAS}

    def search(self, query: str, domain_key: str, limit: int) -> list[Candidate]:
        """Text mode: preserve the query, with no geographic constraint."""
        if not self.text_enabled or limit <= 0:
            return []
        params = {**self._base_params(), "text": query}
        data = self._get({**params, "page": 1})
        return self._pages(params, data, query, domain_key, min(limit, SEARCH_LIMIT))

    def geo_sweep(self) -> list[Candidate]:
        """Return every result in the Malawi box, independent of queries and quotas."""
        if "geo-sweep" not in self.cfg.flickr.modes:
            return []
        box = self.cfg.malawi_bbox
        params = {**self._base_params(), "has_geo": 1,
                  "bbox": f"{box.min_lon},{box.min_lat},{box.max_lon},{box.max_lat}"}
        # Freeze the upper bound for this run; upload ranges are inclusive, in seconds.
        return self._sweep_range(params, 0, int(time.time()))

    def _sweep_range(self, params: dict[str, Any], start: int, end: int) -> list[Candidate]:
        params = {**params, "min_upload_date": start, "max_upload_date": end}
        data = self._get({**params, "page": 1})
        total = int(data["photos"]["total"])
        if total > SEARCH_LIMIT:
            if start == end:
                raise RuntimeError("Flickr has over 4,000 results in one upload second; "
                                   "cannot retrieve a complete sweep by date splitting")
            mid = (start + end) // 2
            # Newest interval first, retaining date-posted-desc ordering globally.
            return (self._sweep_range(params, mid + 1, end)
                    + self._sweep_range(params, start, mid))
        return self._pages(params, data, "", "unassigned", total)

    def _pages(self, params: dict[str, Any], first: dict[str, Any], query: str,
               domain_key: str, limit: int) -> list[Candidate]:
        total = min(int(first["photos"]["total"]), limit)
        out: list[Candidate] = []
        data, page = first, 1
        while len(out) < total:
            found = parse_response(data, query, domain_key, self.licences or {})
            if not found:
                raise RuntimeError("Flickr returned an empty page before the reported total")
            out.extend(found)
            if len(out) >= total:
                break
            page += 1
            data = self._get({**params, "page": page})
        return out[:total]


# =======
# PARSING
# =======


def _float(value: Any) -> float | None:
    try:
        coordinate = float(value)
        return coordinate if coordinate != 0 else None
    except (TypeError, ValueError):
        return None


def parse_response(data: dict[str, Any], query: str, domain_key: str,
                   licences: dict[str, tuple[str, str]]) -> list[Candidate]:
    out = []
    for photo in data.get("photos", {}).get("photo", []):
        size = next((s for s in ("o", "l", "z") if photo.get(f"url_{s}")), "")
        licence, licence_url = licences.get(str(photo.get("license")), ("", ""))
        description = photo.get("description") or {}
        if isinstance(description, dict):
            description = description.get("_content", "")
        description = html.unescape(re.sub(r"<[^>]+>", " ", description)).strip()
        out.append(Candidate(
            source="flickr", source_id=str(photo["id"]), query=query, query_domain=domain_key,
            title=photo.get("title") or "", description=description,
            tags=(photo.get("tags") or "").split(), creator=photo.get("ownername") or photo.get("owner") or "",
            licence=licence, licence_url=licence_url,
            source_url=f"https://www.flickr.com/photos/{photo['owner']}/{photo['id']}/",
            image_url=photo.get(f"url_{size}") or "",
            thumb_url=photo.get("url_z") or photo.get(f"url_{size}") or "",
            mime_type="image/jpeg" if size in {"l", "z"} else {
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp",
            }.get(photo.get("originalformat", "").lower(), ""),
            width=int(photo.get(f"width_{size}") or photo.get("o_width") or 0),
            height=int(photo.get(f"height_{size}") or photo.get("o_height") or 0),
            lat=_float(photo.get("latitude")), lon=_float(photo.get("longitude")),
            date_taken=photo.get("datetaken") or "",
        ))
    return out
