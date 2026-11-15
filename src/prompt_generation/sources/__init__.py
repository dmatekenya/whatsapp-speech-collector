"""Image sources. Each source turns a search query into a list of Candidate records."""

from __future__ import annotations

from typing import Protocol

from ..models import Candidate


class Source(Protocol):
    name: str

    def search(self, query: str, domain_key: str, limit: int) -> list[Candidate]:
        """Return up to `limit` candidates for the query, metadata only (no downloads)."""
        ...


def build_source(name: str, http, cfg) -> Source:
    """Create a source by name. New sources are registered here."""
    if name == "commons":
        from .commons import CommonsSource

        return CommonsSource(http, thumb_width=cfg.thumb_width)
    raise ValueError(f"Unknown source {name!r}; available: commons")
