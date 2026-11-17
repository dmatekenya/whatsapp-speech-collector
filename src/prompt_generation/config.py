"""Load and validate config/prompt-generation.yaml."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# ======
# MODELS
# ======


@dataclass(frozen=True)
class Domain:
    key: str
    label: str
    quota: int
    queries: list[str]
    location: str = "malawi"  # "malawi": must show a Malawi signal; "any": no requirement


@dataclass(frozen=True)
class BBox:
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float

    def contains(self, lat: float | None, lon: float | None) -> bool:
        if lat is None or lon is None:
            return False
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon


@dataclass(frozen=True)
class HttpSettings:
    timeout_seconds: float = 30
    pause_seconds: float = 1.0
    max_retries: int = 4
    contact_email_env: str = "HARVEST_CONTACT_EMAIL"


@dataclass(frozen=True)
class FlickrConfig:
    modes: list[str] = field(default_factory=lambda: ["geo-sweep", "text"])
    api_key_env: str = "FLICKR_API_KEY"

    def __post_init__(self):
        if any(mode not in {"geo-sweep", "text"} for mode in self.modes):
            raise ValueError("flickr.modes must contain only 'geo-sweep' and/or 'text'")
        if len(self.modes) != len(set(self.modes)):
            raise ValueError("flickr.modes must not contain duplicates")


@dataclass(frozen=True)
class HarvestConfig:
    output_root: Path
    cache_dir: Path
    target_multiplier: float
    min_width: int
    min_height: int
    min_year: int
    thumb_width: int
    phash_max_distance: int
    allow_non_commercial: bool
    http: HttpSettings
    malawi_bbox: BBox
    malawi_place_names: list[str]
    unwanted_keywords: list[str]
    allowed_mime_types: list[str]
    domains: list[Domain] = field(default_factory=list)
    max_per_creator_day: int = 0
    max_per_creator_domain: int = 0
    flickr: FlickrConfig = field(default_factory=FlickrConfig)

    def domain(self, key: str) -> Domain:
        for d in self.domains:
            if d.key == key:
                return d
        raise KeyError(f"Unknown domain key: {key!r}")

    def target(self, key: str) -> int:
        """Number of candidates to aim for in a domain (quota x multiplier, rounded up)."""
        return math.ceil(self.domain(key).quota * self.target_multiplier)


# =======
# LOADING
# =======


def load_config(path: str | Path) -> HarvestConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    domains = [
        Domain(
            key=d["key"],
            label=d["label"],
            quota=int(d["quota"]),
            queries=list(d.get("queries", [])),
            location=d.get("location", "malawi"),
        )
        for d in raw["domains"]
    ]
    for d in domains:
        if d.location not in ("malawi", "any"):
            raise ValueError(f"Domain {d.key!r}: location must be 'malawi' or 'any'")

    keys = [d.key for d in domains]
    if len(keys) != len(set(keys)):
        raise ValueError("Domain keys must be unique")
    for k in keys:
        if k != k.lower() or " " in k or "_" in k:
            raise ValueError(f"Domain key {k!r} must be lowercase with hyphens")

    return HarvestConfig(
        output_root=Path(raw["output_root"]),
        cache_dir=Path(raw["cache_dir"]),
        target_multiplier=float(raw.get("target_multiplier", 1.5)),
        min_width=int(raw.get("min_width", 800)),
        min_height=int(raw.get("min_height", 600)),
        min_year=int(raw.get("min_year", 0)),
        thumb_width=int(raw.get("thumb_width", 512)),
        phash_max_distance=int(raw.get("phash_max_distance", 6)),
        allow_non_commercial=bool(raw.get("allow_non_commercial", False)),
        http=HttpSettings(**raw.get("http", {})),
        malawi_bbox=BBox(**raw["malawi_bbox"]),
        malawi_place_names=[p.lower() for p in raw.get("malawi_place_names", [])],
        unwanted_keywords=[w.lower() for w in raw.get("unwanted_keywords", [])],
        allowed_mime_types=list(raw.get("allowed_mime_types", [])),
        domains=domains,
        max_per_creator_day=int(raw.get("max_per_creator_day", 0)),
        max_per_creator_domain=int(raw.get("max_per_creator_domain", 0)),
        flickr=FlickrConfig(**raw.get("flickr", {})),
    )
