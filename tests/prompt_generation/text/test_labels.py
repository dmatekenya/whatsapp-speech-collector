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
    labels, error = parse_reply(json.dumps([reply("A", **bad_fields)]), ["A"], settings)
    assert labels == {}
    assert error


def test_parse_reply_accepts_other_and_invalidates_duplicate_ids():
    settings = load_label_settings()
    payload = [reply("A"), reply("A"), reply("B", topic="other", suggested_topic=" School   Meals ")]
    labels, error = parse_reply(json.dumps(payload), ["A", "B"], settings)
    assert "Duplicate pool ID" in error
    assert labels == {
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
        response = next(self.responses)
        text = response if isinstance(response, str) else json.dumps(response)
        return {"choices": [{"message": {"content": text}}], "usage": {"cost": 0.01}}


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
    retry_client = FakeClient([[reply("C")]], capsys)
    run_labels(tmp_path, settings, client=retry_client)
    assert len(retry_client.requests) == 1
    retry_request = retry_client.requests[0][1][1]["content"]
    assert json.loads(retry_request.split("\n", 1)[1]) == [{"id": "C", "text": "C"}]
    run_labels(tmp_path, settings, client=retry_client)
    assert len(retry_client.requests) == 1

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


@pytest.mark.parametrize("envelope", [
    'Here are the labels [1].\n```json\n{}\n```\nEnd of answer.',
    'Here are the labels.\n{{"results": {}, "warnings": ["example metadata"]}}\nEnd of answer.',
])
def test_parse_reply_accepts_fences_prose_and_wrapped_lists(envelope):
    settings = load_label_settings()
    text = envelope.format(json.dumps([reply("A")]))
    labels, error = parse_reply(text, ["A"], settings)
    assert error is None
    assert labels["A"]["topic"] == "cooking"


def test_parse_reply_matches_pool_ids_and_falls_back_to_position_and_maps_labels():
    settings = load_label_settings()
    items = [
        {"pool_id": "C", "domain": " Work AND trade ", "topic": "Shopping and buying", "confidence": 0.8},
        {"domain": "Food", "topic": "Cooking", "confidence": 0.9},
        reply("A"),
    ]
    labels, error = parse_reply(json.dumps(items), ["A", "B", "C"], settings)
    assert error is None
    assert labels["A"]["confidence"] == 0.8
    assert labels["B"]["topic"] == "cooking"
    assert labels["B"]["confidence"] == 0.9
    assert (labels["C"]["domain"], labels["C"]["topic"]) == ("work-trade", "shopping")


def test_logs_every_reply_and_prints_only_first_failure(tmp_path, capsys):
    _write_csv(tmp_path / "pool.csv", [
        {"pool_id": key, "status": "available", "text": key} for key in ["A", "B"]
    ], POOL_COLUMNS)
    settings = load_label_settings()
    settings = replace(settings, stage3=replace(settings.stage3, batch_size=1))
    bad = ["first malformed reply", "retry malformed reply"]
    client = FakeClient([*bad, [reply("B")]], capsys)
    run_labels(tmp_path, settings, client=client)

    logged = [json.loads(line) for line in (tmp_path / "label-responses.jsonl").read_text().splitlines()]
    assert [(r["batch_number"], r["attempt"], r["pool_ids"]) for r in logged] == [
        (1, 1, ["A"]), (1, 2, ["A"]), (2, 1, ["B"]),
    ]
    assert [r["raw_text"] for r in logged[:2]] == bad
    assert all(r["parse_error"] for r in logged[:2])
    assert logged[2]["raw_text"] == json.dumps([reply("B")])
    assert logged[2]["parse_error"] is None

    printed = capsys.readouterr().out
    assert bad[0] in printed
    assert logged[0]["parse_error"] in printed
    assert bad[1] not in printed

    run_labels(tmp_path, settings, client=FakeClient([[reply("A")]], capsys))
    assert len((tmp_path / "label-responses.jsonl").read_text().splitlines()) == 4
