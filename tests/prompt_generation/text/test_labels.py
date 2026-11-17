import json
from dataclasses import replace

import pytest

from src.prompt_generation.text.domains import read_kept
from src.prompt_generation.text.labels import load_label_settings, parse_reply, run_labels
from src.prompt_generation.text.pool import POOL_COLUMNS
from src.prompt_generation.text.sentences import _write_csv


def reply(row_id, **fields):
    return {"id": row_id, "domain": "food", "topic": "cooking", "confidence": 0.8, **fields}


@pytest.mark.parametrize("bad_fields", [
    {"domain": "unclear"}, {"domain": []}, {"topic": "football"}, {"topic": []},
    {"confidence": 1.1}, {"confidence": "NaN"}, {"confidence": True},
    {"topic": "other", "suggested_topic": "single"}, {"topic": "other", "suggested_topic": "987 words"},
    {"topic": "other", "suggested_topic": "one two three four five"},
])
def test_parse_reply_rejects_invalid_label_fields(bad_fields):
    settings = load_label_settings()
    assert parse_reply(json.dumps([reply("A", **bad_fields)]), {"A"}, settings.topics) == {}


def test_parse_reply_accepts_other_and_invalidates_duplicate_ids():
    settings = load_label_settings()
    payload = [reply("A"), reply("A"), reply("B", topic="other", suggested_topic=" School   Meals ")]
    assert parse_reply(json.dumps(payload), {"A", "B"}, settings.topics) == {
        "B": {"domain": "food", "topic": "other", "suggested_topic": "school meals", "confidence": 0.8}
    }
    assert "Peeling, pounding maize" in settings.system_prompt
    assert '"unclear"' not in settings.system_prompt


class FakeClient:
    def __init__(self, responses, capsys):
        self.responses = iter(responses)
        self.requests = []
        self.capsys = capsys

    def chat(self, model, messages):
        if not self.requests:
            assert "Estimated cost" in self.capsys.readouterr().out
        self.requests.append((model, messages))
        return {"choices": [{"message": {"content": json.dumps(next(self.responses))}}], "usage": {"cost": 0.01}}


def test_labels_retry_only_failed_rows_and_resume_by_model_and_version(tmp_path, capsys):
    _write_csv(tmp_path / "pool.csv", [
        {"pool_id": key, "status": status, "text": key}
        for key, status in [("A", "available"), ("B", "active"), ("C", "available"), ("R", "retired")]
    ], POOL_COLUMNS)
    settings = load_label_settings()
    client = FakeClient([
        [reply("A"), reply("B", topic="football")],
        [reply("B")],
    ], capsys)
    run_labels(tmp_path, settings, client=client)
    assert len(client.requests) == 2
    assert '"id": "R"' not in client.requests[0][1][1]["content"]
    assert '"id": "B"' in client.requests[1][1][1]["content"]
    assert '"id": "A"' not in client.requests[1][1][1]["content"]
    saved = {r["pool_id"]: r for r in read_kept(tmp_path / "labels-model.csv")}
    assert saved["A"]["topic"] == saved["B"]["topic"] == "cooking"
    assert saved["C"]["domain"] == "invalid"
    run_labels(tmp_path, settings, client=client)
    assert len(client.requests) == 2

    next_client = FakeClient([[reply("C")]], capsys)
    run_labels(tmp_path, replace(settings, version="next"), limit=1, seed=42, client=next_client)
    assert len(next_client.requests) == 1
    saved = {r["pool_id"]: r for r in read_kept(tmp_path / "labels-model.csv")}
    assert saved["C"]["topic_list_version"] == "next"
    assert saved["A"]["topic_list_version"] == settings.version


def test_failed_retry_preserves_successes_and_leaves_pending_rows_for_rerun(tmp_path, capsys):
    _write_csv(tmp_path / "pool.csv", [
        {"pool_id": key, "status": "available", "text": key} for key in ["A", "B"]
    ], POOL_COLUMNS)
    settings = load_label_settings()

    class FailingRetry(FakeClient):
        def chat(self, model, messages):
            if self.requests:
                raise RuntimeError("transport failed")
            return super().chat(model, messages)

    with pytest.raises(RuntimeError, match="transport failed"):
        run_labels(tmp_path, settings, client=FailingRetry([[reply("A")]], capsys))
    assert [row["pool_id"] for row in read_kept(tmp_path / "labels-model.csv")] == ["A"]

    next_client = FakeClient([[reply("B")]], capsys)
    run_labels(tmp_path, settings, client=next_client)
    assert [row["pool_id"] for row in read_kept(tmp_path / "labels-model.csv")] == ["A", "B"]
    assert '"id": "A"' not in next_client.requests[0][1][1]["content"]
