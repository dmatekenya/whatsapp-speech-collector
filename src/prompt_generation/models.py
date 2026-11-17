"""The Candidate record: one harvested image, one row in candidates.csv."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, fields


@dataclass
class Candidate:
    # Identity
    source: str
    source_id: str
    query: str
    query_domain: str

    # Source metadata
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    creator: str = ""
    licence: str = ""
    licence_url: str = ""
    source_url: str = ""      # landing page for attribution and removal requests
    image_url: str = ""       # original file
    thumb_url: str = ""       # resized copy used for checks
    mime_type: str = ""
    width: int = 0
    height: int = 0
    lat: float | None = None
    lon: float | None = None
    date_taken: str = ""

    # Filled by filters and checks
    malawi_signal: str = "none"   # geo | category | text | none
    region_signal: str = "none"   # malawi | africa | none
    thumb_path: str = ""
    thumb_sha256: str = ""
    phash: str = ""
    has_text: str = ""            # "", "yes", "no" ("" = OCR not run)
    detected_text: str = ""
    status: str = "kept"          # kept | dropped
    drop_reason: str = ""

    @property
    def candidate_id(self) -> str:
        """Stable ID: the same source image always gets the same ID across runs."""
        return hashlib.sha1(f"{self.source}:{self.source_id}".encode()).hexdigest()[:16]

    def drop(self, reason: str) -> None:
        if self.status != "dropped":
            self.status = "dropped"
            self.drop_reason = reason

    def to_row(self) -> dict[str, str]:
        row = {"candidate_id": self.candidate_id}
        for key, value in asdict(self).items():
            if isinstance(value, list):
                row[key] = "|".join(value)
            elif value is None:
                row[key] = ""
            else:
                row[key] = str(value)
        return row


CSV_FIELDS = ["candidate_id"] + [f.name for f in fields(Candidate)]
