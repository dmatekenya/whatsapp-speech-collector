import csv
from collections import Counter
from dataclasses import replace

import pytest

from scripts.prompt_generation.make_labelling_batch import main
from src.prompt_generation.text.domains import read_kept
from src.prompt_generation.text.labels import LABEL_COLUMNS, load_label_settings
from src.prompt_generation.text.pool import POOL_COLUMNS
from src.prompt_generation.text.review import (
    MANIFEST_COLUMNS,
    allocate,
    allocate_target_share,
    gap_counts,
    write_gaps,
    write_sentence_batch,
)
from src.prompt_generation.text.sentences import _write_csv


def test_gaps_count_active_live_even_if_unrecorded(tmp_path):
    settings = load_label_settings()
    settings = replace(settings, targets={**settings.targets, "food": 1, "family": 2})
    rows = [
        {"pool_id": key, "origin": origin, "status": status, "in_use": False}
        for key, origin, status in [
            ("L1", "live", "active"), ("L2", "live", "active"), ("R", "live", "retired"),
            ("N", "new", "available"), ("U", "live", "active"),
        ]
    ]
    labels = {
        key: {"pool_id": key, "domain": "food", "topic": "cooking", "confidence": 0.9,
              "model": settings.stage3.model, "topic_list_version": settings.version}
        for key in ["L1", "L2", "R", "N"]
    }
    domains, topics = gap_counts(rows, labels, settings)
    assert {row["domain"]: row["gap"] for row in domains}["family"] == 2
    assert next(row for row in topics if row["topic"] == "cooking") == {
        "domain": "food", "topic": "cooking", "live_active": 2, "new_available": 1,
    }
    _write_csv(tmp_path / "pool.csv", rows, POOL_COLUMNS)
    _write_csv(tmp_path / "labels-model.csv", list(labels.values()), LABEL_COLUMNS)
    write_gaps(tmp_path, settings)
    assert next(row for row in read_kept(tmp_path / "gaps.csv") if row["domain"] == "food") == {
        "domain": "food", "target": "1", "live_active": "2", "new_available": "1", "gap": "0",
    }
    assert len(read_kept(tmp_path / "gaps-by-topic.csv")) == 86


def test_allocation_minimums_caps_and_largest_remaining_gap():
    domains = [
        {"domain": key, "gap": gap, "new_available": available}
        for key, gap, available in [("a", 900, 20), ("b", 400, 400), ("c", 200, 200), ("d", 0, 60)]
    ]
    assert allocate(domains, 250) == {"a": 20, "b": 130, "c": 50, "d": 50}
    assert allocate(domains, 2) == {"a": 1, "b": 1, "c": 0, "d": 0}
    assert allocate(domains, 1000) == {"a": 20, "b": 400, "c": 200, "d": 60}


def test_target_share_rounds_by_largest_remainder():
    targets = {"a": 50, "b": 30, "c": 20}
    places, final = allocate_target_share(targets, dict.fromkeys(targets, 10), 7)
    assert places == final == {"a": 4, "b": 2, "c": 1}


@pytest.mark.parametrize(("size", "expected"), [
    (20, {"a": 2, "b": 7, "c": 7, "d": 4}),
    (100, {"a": 2, "b": 7, "c": 10, "d": 10}),
])
def test_target_share_caps_and_redistributes_repeatedly(size, expected):
    targets = {"a": 40, "b": 30, "c": 20, "d": 10}
    available = {"a": 2, "b": 7, "c": 10, "d": 10}
    places, final = allocate_target_share(targets, available, size)
    assert sum(places.values()) == size
    assert final == expected


@pytest.mark.parametrize("options", [[], ["--allocation", "gap"]])
def test_sentence_cli_writes_seeded_blind_manifest_and_protects_ready(tmp_path, monkeypatch, capsys, options):
    settings = load_label_settings()
    pool = tmp_path / "pool"
    pool.mkdir()
    rows, labels = [], []
    for i in range(29):
        key = f"P{i:02d}"
        origin = "new" if i < 24 else "live"
        status = "available" if i < 24 else ("retired" if i == 24 else "active")
        rows.append({
            "pool_id": key, "origin": origin, "status": status, "text": f"Text {i}",
            "prompt_id": f"L{i}" if origin == "live" else "",
            "in_use": i == 25, "times_recorded": 2 if i in {26, 28} else (None if i == 25 else 0),
        })
        labels.append({
            "pool_id": key, "domain": "food", "topic": "cooking" if i < 12 else "eating-meal",
            "confidence": 0.9, "model": settings.stage3.model,
            "topic_list_version": "old" if i in {0, 23} else settings.version,
        })
    _write_csv(pool / "pool.csv", rows, [*POOL_COLUMNS, "times_recorded"])
    _write_csv(pool / "labels-model.csv", labels, LABEL_COLUMNS)
    monkeypatch.setattr("src.prompt_generation.text.pool.POOL_ROOT", tmp_path)
    review_root = tmp_path / "review"
    assert main([
        "--kind", "sentences", "--pool-id", "pool", "--batch", "sample", "--size", "18",
        "--review-root", str(review_root), *options,
    ]) == 0
    report = capsys.readouterr().out
    assert "Available candidates by origin" in report
    assert "Target places" in report and "Final places" in report
    assert next(line.split() for line in report.splitlines() if line.strip().startswith("food")) == [
        "food", "1", "22",
    ]
    out = review_root / "sample"
    manifest = read_kept(out / "manifest.csv")
    with (out / "manifest.csv").open() as handle:
        assert csv.DictReader(handle).fieldnames == MANIFEST_COLUMNS
    assert len(manifest) == 18
    assert sum(r["overlap"] == "yes" for r in manifest) == 2
    sidecar = read_kept(out / "model-labels.csv")
    assert Counter(r["topic"] for r in sidecar) == {"cooking": 9, "eating-meal": 9}
    assert (out / "READY").is_file()
    mode = "gap" if options else "target-share"
    write_sentence_batch(pool, tmp_path / "another", settings, size=18, allocation=mode)
    assert read_kept(tmp_path / "another" / "manifest.csv") == manifest
    with pytest.raises(FileExistsError, match="already marked READY"):
        write_sentence_batch(pool, out, settings, size=18)
    assert read_kept(out / "manifest.csv") == manifest
    write_sentence_batch(pool, tmp_path / "all", settings, size=30, allocation=mode)
    all_rows = read_kept(tmp_path / "all" / "manifest.csv")
    assert {(row["candidate_id"], row["origin"]) for row in all_rows} == {
        ("L27", "live"), *((f"P{i:02d}", "new") for i in range(1, 23)),
    }
    assert "read_only" not in all_rows[0]
