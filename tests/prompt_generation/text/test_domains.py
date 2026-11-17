from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.prompt_generation.text.domains import (
    load_domain_settings,
    parse_batch_reply,
    read_kept,
    run_domains,
)
from src.prompt_generation.text.sentences import KEPT_COLUMNS, load_sentence_settings, run_sentences, write_kept

RUN_ID = "run-test"


class FakeClient:
    """Returns queued reply texts in order and records the message lists it was sent."""

    def __init__(self, replies: list[str], cost: float = 0.01) -> None:
        self.replies = list(replies)
        self.cost = cost
        self.sent: list[list[dict[str, str]]] = []

    def chat(self, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.sent.append(messages)
        content = self.replies.pop(0)
        return {"choices": [{"message": {"content": content}}], "usage": {"cost": self.cost}}


def _row(sentence_id: str, domain: str = "") -> dict[str, str]:
    return {column: "" for column in KEPT_COLUMNS} | {
        "sentence_id": sentence_id,
        "text": f"Sentence {sentence_id} lero.",
        "source": "malawi-news",
        "domain": domain,
    }


def _reply(assignments: dict[str, str]) -> str:
    return json.dumps([{"id": sid, "domain": dom, "confidence": 0.9} for sid, dom in assignments.items()])


def _settings(tmp_path: Path, batch_size: int = 2, max_spend: float = 4.0):
    base = load_domain_settings()
    return replace(
        base,
        sentences_root=tmp_path,
        batch_size=batch_size,
        scoring=replace(base.scoring, max_spend_usd=max_spend),
    )


def _write_run(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir(parents=True)
    write_kept(run_dir / "sentences-kept.csv", rows)
    return run_dir / "sentences-kept.csv"


def test_parse_batch_reply_accepts_valid_and_unclear() -> None:
    reply = json.dumps([
        {"id": "a", "domain": "food", "confidence": 0.8},
        {"id": "b", "domain": "unclear", "confidence": 0.2},
    ])
    assert parse_batch_reply(reply, ["a", "b"], {"food", "health"}) == {
        "a": ("food", 0.8),
        "b": ("unclear", 0.2),
    }


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        json.dumps([{"id": "a", "domain": "sport", "confidence": 0.5}]),  # not a domain key
        json.dumps([{"id": "x", "domain": "food", "confidence": 0.5}]),  # unknown id
        json.dumps([{"id": "a", "domain": "food", "confidence": 2}]),  # confidence out of range
        json.dumps([{"id": "a", "domain": "food", "confidence": 0.5}]),  # missing id "b"
    ],
)
def test_parse_batch_reply_rejects_malformed(reply: str) -> None:
    with pytest.raises(ValueError):
        parse_batch_reply(reply, ["a", "b"], {"food"})


def test_run_retries_malformed_batch_once(tmp_path: Path) -> None:
    kept = _write_run(tmp_path, [_row("a"), _row("b"), _row("c")])
    client = FakeClient([
        "not json",  # first batch of two fails and is retried
        _reply({"a": "food", "b": "health"}),
        _reply({"c": "nature"}),
    ])
    summary = run_domains(run_id=RUN_ID, settings=_settings(tmp_path), client=client)

    rows = {row["sentence_id"]: row for row in read_kept(kept)}
    assert (rows["a"]["domain"], rows["a"]["domain_source"]) == ("food", "model")
    assert rows["c"]["domain"] == "nature"
    assert len(client.sent) == 3
    assert summary["scored"] == 3 and summary["unscored"] == 0
    with (kept.parent / "domain-scores.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == [
            "sentence_id", "domain", "domain_confidence", "domain_source", "model", "scored_at",
        ]
        scores = list(reader)
    assert {row["sentence_id"] for row in scores} == {"a", "b", "c"}
    assert all(row["model"] == _settings(tmp_path).model and row["scored_at"] for row in scores)


def test_two_bad_replies_mark_batch_unscored(tmp_path: Path) -> None:
    kept = _write_run(tmp_path, [_row("a")])
    client = FakeClient(["oops", "still oops"])
    summary = run_domains(run_id=RUN_ID, settings=_settings(tmp_path), client=client)

    (row,) = read_kept(kept)
    assert row["domain"] == "" and row["domain_source"] == "unscored"
    assert summary["unscored"] == 1 and summary["errors"]


def test_resume_skips_rows_that_already_have_a_domain(tmp_path: Path) -> None:
    kept = _write_run(tmp_path, [_row("a", domain="food"), _row("b")])
    client = FakeClient([_reply({"b": "transport"})])
    summary = run_domains(run_id=RUN_ID, settings=_settings(tmp_path), client=client)

    rows = {row["sentence_id"]: row for row in read_kept(kept)}
    assert rows["a"]["domain"] == "food" and rows["a"]["domain_source"] == ""
    assert rows["b"]["domain"] == "transport"
    assert len(client.sent) == 1
    assert summary["already_scored"] == 1


def test_spend_cap_stops_before_next_batch(tmp_path: Path) -> None:
    kept = _write_run(tmp_path, [_row("a"), _row("b"), _row("c")])
    client = FakeClient([_reply({"a": "food"}), _reply({"b": "food"})], cost=0.05)
    summary = run_domains(
        run_id=RUN_ID, settings=_settings(tmp_path, batch_size=1, max_spend=0.01), client=client
    )

    assert len(client.sent) == 1
    assert summary["stopped_by_limit"] is True and summary["remaining"] == 2
    domains = [row["domain"] for row in read_kept(kept)]
    assert domains == ["food", "", ""]
    assert "domains" in json.loads((tmp_path / RUN_ID / "run-log.json").read_text(encoding="utf-8"))


def test_sentences_and_domains_restore_persisted_scores_by_id(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted" / RUN_ID
    extracted.mkdir(parents=True)
    doc = {
        "doc_id": "nation-1", "source_category": "nation-publications", "source_reference": "Nation Publications",
        "method": "wxr", "source_file": "nation.xml",
        "blocks": ["Mwana " + "wake " * 13 + "lero.", "Amayi " + "awo " * 16 + "lero."],
    }
    (extracted / "documents.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")
    sentences_settings = replace(
        load_sentence_settings(), extracted_text_root=tmp_path / "extracted",
        sentences_root=tmp_path, existing_sentences=tmp_path / "missing.csv",
    )
    run_sentences(run_id=RUN_ID, settings=sentences_settings)
    kept = tmp_path / RUN_ID / "sentences-kept.csv"
    ids = [row["sentence_id"] for row in read_kept(kept)]
    client = FakeClient([_reply(dict(zip(ids, ["food", "health"])))])
    run_domains(run_id=RUN_ID, settings=_settings(tmp_path), client=client)
    scores_path = kept.parent / "domain-scores.csv"
    saved_scores = scores_path.read_bytes()

    # The durable file restores scores even when the kept CSV has none, in a different order.
    write_kept(kept, [_row(sid) for sid in reversed(ids)])
    no_calls = FakeClient([])
    summary = run_domains(run_id=RUN_ID, settings=_settings(tmp_path), client=no_calls)
    assert summary["already_scored"] == 2 and no_calls.sent == []
    assert {row["sentence_id"]: row["domain"] for row in read_kept(kept)} == dict(zip(ids, ["food", "health"]))

    # Removing a unit doesn't erase its score; bringing it back restores the original result.
    original_blocks = doc["blocks"]
    doc["blocks"] = original_blocks[1:]
    (extracted / "documents.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")
    run_sentences(run_id=RUN_ID, settings=sentences_settings)
    assert len(read_kept(kept)) == 1
    doc["blocks"] = original_blocks
    (extracted / "documents.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")
    run_sentences(run_id=RUN_ID, settings=sentences_settings)
    assert [row["domain"] for row in read_kept(kept)] == ["food", "health"]
    assert [row["domain_confidence"] for row in read_kept(kept)] == ["0.90", "0.90"]
    assert all(row["domain_source"] == "model" for row in read_kept(kept))
    assert scores_path.read_bytes() == saved_scores


def test_sentences_migrates_legacy_domain_results(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted" / RUN_ID
    extracted.mkdir(parents=True)
    text = "Mwana " + "wake " * 13 + "lero."
    (extracted / "documents.jsonl").write_text(json.dumps({
        "doc_id": "nation-1", "source_category": "nation-publications", "source_reference": "Nation Publications",
        "method": "wxr", "source_file": "nation.xml", "blocks": [text],
    }) + "\n", encoding="utf-8")
    settings = replace(
        load_sentence_settings(), extracted_text_root=tmp_path / "extracted",
        sentences_root=tmp_path, existing_sentences=tmp_path / "missing.csv",
    )
    run_sentences(run_id=RUN_ID, settings=settings)
    kept = tmp_path / RUN_ID / "sentences-kept.csv"
    rows = read_kept(kept)
    rows[0].update(domain="food", domain_confidence="0.85", domain_source="model")
    write_kept(kept, rows)
    (kept.parent / "domain-scores.csv").unlink()
    run_sentences(run_id=RUN_ID, settings=settings)
    assert read_kept(kept)[0]["domain"] == "food"
    with (kept.parent / "domain-scores.csv").open(encoding="utf-8") as handle:
        (score,) = csv.DictReader(handle)
    assert score["domain_confidence"] == "0.85"
    assert score["model"] == "" and score["scored_at"] == ""
