"""Run a harvest: search sources, filter, check thumbnails, write candidates.csv."""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import checks, filters
from .config import HarvestConfig
from .models import CSV_FIELDS, Candidate
from .sources import Source

log = logging.getLogger(__name__)


@dataclass
class HarvestResult:
    run_dir: Path
    candidates: list[Candidate] = field(default_factory=list)

    def summary(self) -> list[dict[str, object]]:
        """One row per (domain, source): harvested, kept and drop counts by reason."""
        rows: dict[tuple[str, str], dict[str, object]] = {}
        for c in self.candidates:
            row = rows.setdefault(
                (c.query_domain, c.source),
                {"domain": c.query_domain, "source": c.source, "harvested": 0, "kept": 0,
                 "dropped": Counter()},
            )
            row["harvested"] += 1
            if c.status == "kept":
                row["kept"] += 1
            else:
                row["dropped"][c.drop_reason.split(":")[0]] += 1
        return [rows[k] for k in sorted(rows)]


# ===
# RUN
# ===


def run_harvest(
    cfg: HarvestConfig,
    sources: list[Source],
    domain_keys: list[str],
    run_id: str,
    limit_per_query: int,
    download: checks.Downloader,
    ocr: bool = False,
) -> HarvestResult:
    run_dir = cfg.output_root / run_id
    thumbs_dir = run_dir / "thumbs"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Search (metadata only)
    candidates: list[Candidate] = []
    for source in sources:
        sweep = getattr(source, "geo_sweep", None)
        if sweep is not None:
            found = sweep()
            log.info("unassigned | %s | geo-sweep -> %d", source.name, len(found))
            candidates.extend(found)
    for key in domain_keys:
        domain = cfg.domain(key)
        for source in sources:
            if not getattr(source, "text_enabled", True):
                continue
            for query in domain.queries:
                try:
                    found = source.search(query, key, limit_per_query)
                except Exception as exc:
                    log.error("%s search failed for %r: %s", source.name, query, exc)
                    continue
                log.info("%s | %s | %r -> %d", key, source.name, query, len(found))
                candidates.extend(found)

    # 2. Metadata filters and exact duplicates
    filters.apply_metadata_filters(candidates, cfg)
    filters.drop_exact_duplicates(candidates)
    filters.cap_per_creator_day(candidates, cfg.max_per_creator_day)
    filters.cap_per_creator_domain(candidates, cfg.max_per_creator_domain)

    # 3. Thumbnails, hashes, near-duplicates, optional OCR
    checks.fetch_thumbnails(candidates, thumbs_dir, download)
    checks.drop_near_duplicates(candidates, cfg.phash_max_distance)
    if ocr:
        checks.detect_text(candidates)

    result = HarvestResult(run_dir=run_dir, candidates=candidates)
    _write_outputs(result, cfg, domain_keys, [s.name for s in sources], run_id,
                   limit_per_query, ocr)
    return result


# =======
# OUTPUTS
# =======


def _write_outputs(result, cfg, domain_keys, source_names, run_id, limit_per_query, ocr):
    csv_path = result.run_dir / "candidates.csv"
    tmp = csv_path.with_suffix(".csv.part")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for c in result.candidates:
            writer.writerow(c.to_row())
    tmp.replace(csv_path)

    summary = [
        {**row, "dropped": dict(row["dropped"]),
         "target": None if row["domain"] == "unassigned" else cfg.target(row["domain"])}
        for row in result.summary()
    ]
    log_data = {
        "run_id": run_id,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": source_names,
        "domains": domain_keys,
        "limit_per_query": limit_per_query,
        "ocr": ocr,
        "allow_non_commercial": cfg.allow_non_commercial,
        "summary": summary,
    }
    (result.run_dir / "run-log.json").write_text(json.dumps(log_data, indent=2), encoding="utf-8")


def format_summary(result: HarvestResult, cfg: HarvestConfig) -> str:
    lines = [f"{'domain':<16}{'source':<10}{'harvested':>10}{'kept':>7}{'target':>8}  dropped"]
    for row in result.summary():
        dropped = ", ".join(f"{k}={v}" for k, v in row["dropped"].most_common()) or "-"
        target = "-" if row["domain"] == "unassigned" else str(cfg.target(row["domain"]))
        lines.append(
            f"{row['domain']:<16}{row['source']:<10}{row['harvested']:>10}{row['kept']:>7}"
            f"{target:>8}  {dropped}"
        )
    return "\n".join(lines)
