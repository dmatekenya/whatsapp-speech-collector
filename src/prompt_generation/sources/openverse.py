"""Openverse image metadata, excluding Wikimedia (harvested directly).

API parameters: https://api.openverse.org/v1/
Page limits: https://docs.openverse.org/projects/proposals/proxy_frontend_api_requests/20241202-implementation_plan_proxy_frontend_api_requests_ip.html
Only per-request headers carry the optional token; downloads never receive it.
"""

from __future__ import annotations

import os
from typing import Any

from ..config import HarvestConfig
from ..models import Candidate

API_URL = "https://api.openverse.org/v1/images/"
ANONYMOUS_PAGE_SIZE = 20
AUTHENTICATED_PAGE_SIZE = 50


class OpenverseSource:
    name = "openverse"

    def __init__(self, http, cfg: HarvestConfig):
        self.http = http
        self.cfg = cfg
        self.token = os.environ.get("OPENVERSE_TOKEN", "").strip()

    # ======
    # SEARCH
    # ======

    def search(self, query: str, domain_key: str, limit: int) -> list[Candidate]:
        candidates: list[Candidate] = []
        page = 1
        max_size = AUTHENTICATED_PAGE_SIZE if self.token else ANONYMOUS_PAGE_SIZE
        size = min(max_size, limit)
        while len(candidates) < limit:
            params = {"q": query, "page": page, "page_size": size,
                      "excluded_source": "wikimedia"}
            if not self.cfg.allow_non_commercial:
                params["license_type"] = "commercial"
            if self.token:
                data = self.http.get_json(
                    self.name, API_URL, params,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
            else:
                data = self.http.get_json(self.name, API_URL, params)
            found = parse_response(data, query, domain_key)
            candidates.extend(found)
            if not found:
                break
            page_count = data.get("page_count")
            if page_count is not None:
                if page >= int(page_count):
                    break
            elif not data.get("next"):
                break
            page += 1
        return candidates[:limit]


# =======
# PARSING
# =======


def parse_response(data: dict[str, Any], query: str, domain_key: str) -> list[Candidate]:
    """Map Openverse records into the shared candidate schema, preserving rank order."""
    out: list[Candidate] = []
    for result in data.get("results", []):
        licence = (result.get("license") or "").lower()
        if licence == "cc0":
            label = "CC0"
        elif licence == "pdm":
            label = "Public domain"
        elif licence:
            label = f"CC {licence.upper()} {result.get('license_version') or ''}".strip()
        else:
            label = ""
        tags = [t["name"] for t in result.get("tags") or [] if t.get("name")]
        provider = result.get("provider") or result.get("source")
        if provider:
            tags.append(f"provider:{provider}")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp"}.get((result.get("filetype") or "").lower(), "")
        out.append(Candidate(
            source="openverse", source_id=str(result["id"]), query=query,
            query_domain=domain_key, title=result.get("title") or "",
            description=result.get("description") or "", tags=tags,
            creator=result.get("creator") or "", source_url=result.get("foreign_landing_url") or "",
            image_url=result.get("url") or "", thumb_url=result.get("thumbnail") or "",
            mime_type=mime, width=int(result.get("width") or 0),
            height=int(result.get("height") or 0), date_taken=result.get("date_taken") or "",
            licence=label, licence_url=result.get("license_url") or "",
        ))
    return out
