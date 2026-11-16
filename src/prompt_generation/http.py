"""A small HTTP client: polite pauses, retries with backoff, and an on-disk response cache.

The cache means a re-run with the same queries does not call the source APIs again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from .config import HttpSettings

log = logging.getLogger(__name__)

USER_AGENT_BASE = "ChichewaSpeechPromptHarvester/0.1 (ZaZo AI; research prompt collection)"


class HttpClient:
    def __init__(self, settings: HttpSettings, cache_dir: Path, use_cache: bool = True):
        self.settings = settings
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache
        self.session = requests.Session()
        email = os.environ.get(settings.contact_email_env, "").strip()
        if not email:
            log.warning(
                "%s is not set; Wikimedia asks API users to include contact details "
                "in the User-Agent", settings.contact_email_env,
            )
        agent = f"{USER_AGENT_BASE} {email}".strip()
        self.session.headers["User-Agent"] = agent
        self._last_call = 0.0

    # =========
    # JSON GETS
    # =========

    def get_json(self, source: str, url: str, params: dict[str, Any],
                 headers: dict[str, str] | None = None) -> dict[str, Any]:
        cache_file = self._cache_path(source, url, params)
        if self.use_cache and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        response = self._request(url, params=params, headers=headers)
        data = response.json()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    # =========
    # DOWNLOADS
    # =========

    def download(self, url: str, dest: Path) -> Path:
        """Download a file once; an existing file is reused."""
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        response = self._request(url, stream=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in response.iter_content(chunk_size=65536):
                fh.write(chunk)
        tmp.replace(dest)
        return dest

    # ========
    # INTERNAL
    # ========

    def _request(self, url: str, **kwargs: Any) -> requests.Response:
        delay = 2.0
        for attempt in range(self.settings.max_retries + 1):
            self._pause()
            try:
                response = self.session.get(url, timeout=self.settings.timeout_seconds, **kwargs)
            except requests.RequestException as exc:
                if attempt == self.settings.max_retries:
                    raise
                log.warning("Request failed (%s); retrying in %.0fs", exc, delay)
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.settings.max_retries:
                    response.raise_for_status()
                wait = float(response.headers.get("Retry-After", delay))
                log.warning("HTTP %s from %s; waiting %.0fs", response.status_code, url, wait)
                time.sleep(wait)
                delay *= 2
                continue

            response.raise_for_status()
            return response
        raise RuntimeError("unreachable")

    def _pause(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.settings.pause_seconds:
            time.sleep(self.settings.pause_seconds - elapsed)
        self._last_call = time.monotonic()

    def _cache_path(self, source: str, url: str, params: dict[str, Any]) -> Path:
        key = json.dumps({"url": url, "params": params}, sort_keys=True)
        digest = hashlib.sha1(key.encode()).hexdigest()
        return self.cache_dir / source / f"{digest}.json"
