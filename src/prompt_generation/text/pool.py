"""Combine the production export and stage 2 units without changing either input."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from fcntl import LOCK_EX, flock
from pathlib import Path

from src.config import PROJECT_ROOT
from src.prompt_generation.text.domains import read_kept
from src.prompt_generation.text.sentences import _write_csv, dedupe_key

TEXT_ROOT = PROJECT_ROOT / "data/prompts/text"
POOL_ROOT = TEXT_ROOT / "sentence-pool"
FEATURE_COLUMNS = [
    "word_count", "char_count", "avg_word_len", "long_words", "est_read_s", "in_target_length",
]
POOL_COLUMNS = [
    "pool_id", "origin", "prompt_id", "candidate_id", "text", "source_category", "source_ref",
    "status", "in_use", *FEATURE_COLUMNS,
]


def named_directory(root: Path, name: str) -> Path:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError(f"Expected a single directory name, got {name!r}")
    return root / name


@contextmanager
def output_lock(directory: Path):
    """Serialize writers to the same output directory."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".write.lock").open("a") as handle:
        flock(handle, LOCK_EX)
        yield


def features(text: str) -> dict:
    words = text.split()
    count = len(words)
    return {
        "word_count": count,
        "char_count": len(text),
        "avg_word_len": round(sum(map(len, words)) / count, 2) if count else 0,
        "long_words": sum(len(word) > 12 for word in words),
        "est_read_s": round(count / 1.2, 1),
        "in_target_length": 15 <= count <= 55,
    }


def build_pool(live_path: Path, kept_path: Path, out: Path) -> None:
    rows, dropped = [], []
    live_ids = {}
    for row in read_kept(live_path):
        prompt_id = row["prompt_id"]
        active = row["active"] == "t"
        key = dedupe_key(row["text"])
        # Prefer the active ID if the same text also occurs in a retired row.
        if active or key not in live_ids:
            live_ids[key] = prompt_id
        rows.append({
            "pool_id": prompt_id, "origin": "live", "prompt_id": prompt_id, "text": row["text"],
            "source_category": row["source"], "source_ref": row["source_file"],
            "status": "active" if active else "retired",
            "in_use": active and int(row["times_recorded"] or 0) > 0,
            **features(row["text"]),
        })

    for row in read_kept(kept_path):
        candidate_id = row["sentence_id"]
        item = {
            "pool_id": candidate_id, "origin": "new", "candidate_id": candidate_id,
            "text": row["text"], "source_category": row["source"],
            "source_ref": row["source_reference"] or row["source_file"],
            "status": "available", "in_use": False, **features(row["text"]),
        }
        prompt_id = live_ids.get(dedupe_key(row["text"]))
        if prompt_id is None:
            rows.append(item)
        else:
            dropped.append({**item, "prompt_id": prompt_id, "status": "dropped",
                            "drop_reason": "duplicate-of-live"})

    ids = [row["pool_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate pool IDs in the input CSVs")

    with output_lock(out):
        _write_csv(out / "pool-dropped.csv", dropped, [*POOL_COLUMNS, "drop_reason"])
        _write_csv(out / "pool.csv", rows, POOL_COLUMNS)
    print(f"Pool {out.name}: {len(rows)} rows, {len(dropped)} duplicates of live rows")
    for field in ("origin", "status", "source_category"):
        for value, count in sorted(Counter(row[field] for row in rows).items()):
            print(f"  {field} {value}: {count}")
    bands = Counter(
        "under 15" if row["word_count"] < 15 else "over 55" if row["word_count"] > 55 else "15–55"
        for row in rows
    )
    for band in ("under 15", "15–55", "over 55"):
        print(f"  length {band}: {bands[band]}")
