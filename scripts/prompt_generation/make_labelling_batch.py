"""Build a picture or sentence labelling batch for the review platform.

Samples pictures from a harvest run (kept candidates) and the Dollar Street download,
copies them into one folder named by candidate_id, and writes manifest.csv. The READY
marker is written last, so the platform never sees a half-built batch.

Example (from the repo root):
    python3 -m scripts.prompt_generation.make_labelling_batch \
        --harvest-run data/prompts/images/harvest/2026-11-16-pool-v1 \
        --dollar-street data/prompts/images/sources/dollar-street \
        --batch 2026-11-labelling-1 --size 3000

Sentence example:
    uv run python -m scripts.prompt_generation.make_labelling_batch \
        --kind sentences --pool-id text-v2 --batch 2026-11-sentence-review --size 3000

Output goes under data/prompts/images/review or data/prompts/text/review.
"""

from __future__ import annotations

import argparse
import csv
import random
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image

MANIFEST_FIELDS = [
    "candidate_id", "file_name", "source", "source_category", "licence", "licence_url",
    "creator", "source_url", "title", "query_domain", "country", "region_signal",
]
DOLLAR_STREET_URL = "https://www.kaggle.com/datasets/mlcommons/the-dollar-street-dataset"


# ============
# LOAD SOURCES
# ============


def load_harvest(run_dir: Path) -> list[dict]:
    """Kept harvest candidates whose thumbnail exists."""
    items = []
    with open(run_dir / "candidates.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["status"] != "kept":
                continue
            thumb = _first_existing([
                Path(r.get("thumb_path") or ""),
                run_dir / "thumbs" / f"{r['candidate_id']}.jpg",
            ])
            if thumb is None:
                continue
            items.append({
                "candidate_id": r["candidate_id"],
                "path": thumb,
                "stratum": r["query_domain"] or "unassigned",
                "source": r["source"],
                "source_category": r["source"],
                "licence": r.get("licence", ""),
                "licence_url": r.get("licence_url", ""),
                "creator": r.get("creator", ""),
                "source_url": r.get("source_url", ""),
                "title": r.get("title", ""),
                "query_domain": r["query_domain"] or "unassigned",
                "country": "",
                "region_signal": r.get("region_signal") or r.get("malawi_signal", ""),
            })
    return items


def load_dollar_street(ds_dir: Path) -> list[dict]:
    """Downloaded Dollar Street images listed in sampled.csv (flat or nested layout)."""
    sampled = ds_dir / "sampled.csv"
    if not sampled.exists():
        return []
    images = ds_dir / "images"
    items = []
    with open(sampled, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rel = r["imageRelPath"].lstrip("/")
            path = _first_existing([
                images / Path(rel).name,
                images / "dataset_dollarstreet" / rel,
                images / rel,
            ])
            if path is None:
                continue
            topics = r.get("topics", "").strip("[]").replace("'", "")
            items.append({
                "candidate_id": f"ds-{r['id']}",
                "path": path,
                "stratum": "dollar-street",
                "source": "dollar-street",
                "source_category": "dollar-street",
                "licence": "CC BY 4.0",
                "licence_url": "https://creativecommons.org/licenses/by/4.0/",
                "creator": "Gapminder Dollar Street (MLCommons)",
                "source_url": DOLLAR_STREET_URL,
                "title": f"{topics}, {r.get('country.name', '')}".strip(", "),
                "query_domain": "unassigned",
                "country": r.get("country.name", ""),
                "region_signal": "malawi" if r.get("country.name") == "Malawi" else "africa",
            })
    return items


def _first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if str(p) not in ("", ".") and p.is_file():
            return p
    return None


# ========
# SAMPLING
# ========


def sample(items: list[dict], size: int, min_per_stratum: int, seed: int) -> list[dict]:
    """Proportional sample by stratum (search domain, or dollar-street), with a minimum each.

    Strata smaller than the minimum are taken whole. The same seed gives the same sample.
    """
    if size >= len(items):
        return sorted(items, key=lambda i: i["candidate_id"])
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        groups[it["stratum"]].append(it)
    for g in groups.values():
        g.sort(key=lambda i: i["candidate_id"])  # stable order before shuffling

    total = len(items)
    alloc = {k: min(len(g), max(min_per_stratum, round(size * len(g) / total)))
             for k, g in groups.items()}
    # Adjust to hit the requested size: trim the largest allocations or top up from spare.
    while sum(alloc.values()) > size:
        k = max(alloc, key=lambda k: alloc[k] - min_per_stratum)
        if alloc[k] <= min(min_per_stratum, len(groups[k])):
            break
        alloc[k] -= 1
    while sum(alloc.values()) < size:
        spare = [k for k in alloc if alloc[k] < len(groups[k])]
        if not spare:
            break
        k = max(spare, key=lambda k: len(groups[k]) - alloc[k])
        alloc[k] += 1

    rng = random.Random(seed)
    picked = []
    for k in sorted(groups):
        picked += rng.sample(groups[k], alloc[k])
    return picked


# ===========
# WRITE BATCH
# ===========


def write_batch(picked: list[dict], out_dir: Path, max_side: int) -> None:
    if (out_dir / "READY").exists():
        sys.exit(f"{out_dir} is already marked READY; choose a new --batch name")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for it in picked:
        name = f"{it['candidate_id']}.jpg"  # every batch image is a JPEG named by its ID
        dest = out_dir / name
        if not dest.exists():
            _copy_image(it["path"], dest, max_side)
        rows.append({**{k: it.get(k, "") for k in MANIFEST_FIELDS}, "file_name": name})
    with open(out_dir / "manifest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["candidate_id"]))
    (out_dir / "READY").write_text("ready\n", encoding="utf-8")  # written last


def _copy_image(src: Path, dest: Path, max_side: int) -> None:
    """Copy small JPEGs as they are; convert or shrink anything else to a JPEG."""
    with Image.open(src) as img:
        if src.suffix.lower() in (".jpg", ".jpeg") and max(img.size) <= max_side:
            shutil.copy2(src, dest)
            return
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        img.save(dest, "JPEG", quality=88)


# ====
# MAIN
# ====


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kind", choices=("pictures", "sentences"), default="pictures")
    p.add_argument("--harvest-run", type=Path)
    p.add_argument("--pool-id")
    p.add_argument("--live-sample", type=int, default=0)
    p.add_argument("--dollar-street", type=Path)
    p.add_argument("--batch", required=True, help="batch name, e.g. 2026-11-labelling-1")
    p.add_argument("--size", type=int, default=3000)
    p.add_argument("--min-per-stratum", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-side", type=int, default=1280, help="shrink larger images to this")
    p.add_argument("--review-root", type=Path)
    args = p.parse_args(argv)

    if args.kind == "sentences":
        from src.prompt_generation.text.labels import load_label_settings
        from src.prompt_generation.text.pool import POOL_ROOT, TEXT_ROOT, named_directory
        from src.prompt_generation.text.review import write_sentence_batch

        if not args.pool_id:
            p.error("--pool-id is required for sentence batches")
        if args.size < 0 or args.live_sample < 0:
            p.error("--size and --live-sample must be non-negative")
        try:
            write_sentence_batch(
                named_directory(POOL_ROOT, args.pool_id),
                named_directory(args.review_root or TEXT_ROOT / "review", args.batch),
                load_label_settings(), size=args.size, live_sample=args.live_sample, seed=args.seed,
            )
        except (FileExistsError, ValueError) as exc:
            p.error(str(exc))
        return 0
    if not args.harvest_run:
        p.error("--harvest-run is required for picture batches")

    items = load_harvest(args.harvest_run)
    ds = load_dollar_street(args.dollar_street) if args.dollar_street else []
    print(f"pool: {len(items)} harvest + {len(ds)} Dollar Street = {len(items) + len(ds)}")
    picked = sample(items + ds, args.size, args.min_per_stratum, args.seed)

    out = (args.review_root or Path("data/prompts/images/review")) / args.batch
    write_batch(picked, out, args.max_side)
    counts = defaultdict(int)
    for it in picked:
        counts[it["stratum"]] += 1
    print(f"batch {out}: {len(picked)} pictures")
    for k in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {k:<16} {counts[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
