"""Tests for domain scoring and evaluation. No network: the OpenRouter client is faked."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest
from PIL import Image

from src.prompt_generation.picture_evaluation import build_label_sheet, evaluate, format_report
from src.prompt_generation.models import CSV_FIELDS
from src.prompt_generation.picture_scoring import (
    SCORE_FIELDS, load_scoring_config, parse_reply, run_scoring,
)

REPO_ROOT = Path(__file__).parents[2]


# ========
# FIXTURES
# ========


@pytest.fixture
def cfg():
    return load_scoring_config(REPO_ROOT / "config" / "picture-domain-scoring.yaml")


@pytest.fixture
def run_dir(tmp_path):
    """A harvest run with three kept candidates (with thumbnails) and one dropped."""
    run = tmp_path / "run"
    (run / "thumbs").mkdir(parents=True)
    rows = []
    for cid, domain, status in [("a1", "food", "kept"), ("b2", "food", "kept"),
                                ("c3", "transport", "kept"), ("d4", "food", "dropped")]:
        row = {f: "" for f in CSV_FIELDS}
        row.update(candidate_id=cid, query_domain=domain, status=status, title=f"Photo {cid}",
                   tags="Markets in Malawi|Lilongwe")
        if status == "kept":
            Image.new("RGB", (64, 48), "gray").save(run / "thumbs" / f"{cid}.jpg")
        rows.append(row)
    with open(run / "candidates.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return run


class FakeClient:
    """Returns scripted replies per (model, candidate title); records requests."""

    def __init__(self, replies, cost=0.0002):
        self.replies = replies
        self.cost = cost
        self.calls = []

    def chat(self, model, messages):
        text_part = messages[1]["content"][0]["text"]
        found = re.findall(r"Photo ([a-z]\d)", text_part)
        # without metadata the title is not sent; tests then script a single candidate
        cid = found[-1] if found else next(c for m, c in self.replies if m == model)
        self.calls.append((model, cid, messages))
        reply = self.replies[(model, cid)]
        if isinstance(reply, list):  # successive replies for retries
            reply = reply.pop(0)
        return {"choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40, "cost": self.cost}}


def answer(domain, conf=0.9, second=None):
    return json.dumps({"domain": domain, "second_choice": second, "confidence": conf,
                       "reason": "test"})


def read_scores(run):
    with open(run / "domain-scores.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ======
# CONFIG
# ======


def test_rubric_lists_all_twelve_domains(cfg):
    assert len(cfg.domain_keys) == 12
    prompt = cfg.system_prompt()
    for key in cfg.domain_keys:
        assert f"- {key}:" in prompt
    assert "central visible activity" in prompt.lower()


# =======
# PARSING
# =======


def test_parse_reply_accepts_json_in_code_fence(cfg):
    s = parse_reply("```json\n" + answer("food", 0.85, "work-trade") + "\n```", cfg.domain_keys)
    assert (s.domain, s.second_choice, s.confidence) == ("food", "work-trade", 0.85)


def test_parse_reply_rejects_unknown_domain(cfg):
    with pytest.raises(ValueError):
        parse_reply(answer("cooking"), cfg.domain_keys)


def test_parse_reply_accepts_unclear_and_clamps_confidence(cfg):
    s = parse_reply(answer("unclear", 1.7, "nonsense"), cfg.domain_keys)
    assert s.domain == "unclear" and s.confidence == 1.0 and s.second_choice == ""


# =======
# SCORING
# =======


def test_run_scores_kept_only_and_resumes(cfg, run_dir):
    replies = {("m1", "a1"): answer("food"), ("m1", "b2"): answer("work-trade"),
               ("m1", "c3"): answer("transport")}
    client = FakeClient(replies)
    summary = run_scoring(client, cfg, run_dir, ["m1"])
    assert summary["ok"] == 3 and summary["spent_usd"] == pytest.approx(0.0006)
    rows = read_scores(run_dir)
    assert {r["candidate_id"] for r in rows} == {"a1", "b2", "c3"}   # d4 was dropped
    assert list(rows[0].keys()) == SCORE_FIELDS

    # a second run makes no new calls
    again = run_scoring(FakeClient({}), cfg, run_dir, ["m1"])
    assert again["skipped_done"] == 3 and len(read_scores(run_dir)) == 3


def test_metadata_setting_is_sent_and_recorded(cfg, run_dir):
    client = FakeClient({("m1", "a1"): answer("food")})
    run_scoring(client, cfg, run_dir, ["m1"], candidate_ids={"a1"}, include_metadata=False)
    _, _, messages = client.calls[0]
    assert "Source categories" not in messages[1]["content"][0]["text"]
    assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert read_scores(run_dir)[0]["include_metadata"] == "false"


def test_invalid_reply_is_retried_once(cfg, run_dir):
    client = FakeClient({("m1", "a1"): ["I think it is food.", answer("food")],
                         ("m1", "b2"): ["nope", "still nope"]})
    run_scoring(client, cfg, run_dir, ["m1"], candidate_ids={"a1", "b2"})
    by_id = {r["candidate_id"]: r for r in read_scores(run_dir)}
    assert by_id["a1"]["status"] == "ok" and by_id["a1"]["domain"] == "food"
    assert by_id["a1"]["prompt_tokens"] == "1800"          # both attempts counted
    assert by_id["b2"]["status"] == "invalid"


def test_spend_cap_stops_new_requests(cfg, run_dir):
    replies = {("m1", c): answer("food") for c in ("a1", "b2", "c3")}
    client = FakeClient(replies, cost=1.0)
    cfg_serial = cfg.__class__(**{**cfg.__dict__, "concurrency": 1})
    summary = run_scoring(client, cfg_serial, run_dir, ["m1"], max_spend_usd=1.5)
    assert summary["ok"] == 2 and summary["skipped_budget"] == 1


# ==========
# EVALUATION
# ==========


def write_labels(run, labels):
    with open(run / "calibration-labels.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["candidate_id", "label_domain", "notes"])
        for cid, lab in labels.items():
            w.writerow([cid, lab, ""])


def test_label_sheet_lists_kept_candidates_with_existing_labels(cfg, run_dir):
    write_labels(run_dir, {"a1": "food"})
    page = build_label_sheet(run_dir, cfg.domains).read_text(encoding="utf-8")
    assert '"id": "a1"' in page and '"id": "c3"' in page and '"id": "d4"' not in page
    assert '"label": "food"' in page
    assert "thumbs/a1.jpg" in page and '"key": "unclear"' in page


def test_evaluate_reports_accuracy_baseline_and_confusions(cfg, run_dir):
    write_labels(run_dir, {"a1": "food", "b2": "work-trade", "c3": "transport"})
    replies = {("m1", "a1"): answer("food"),
               ("m1", "b2"): answer("food", 0.6, "work-trade"),
               ("m1", "c3"): answer("transport")}
    run_scoring(FakeClient(replies), cfg, run_dir, ["m1"])

    baseline, results = evaluate(run_dir)
    # query domains: a1 food (right), b2 food (wrong), c3 transport (right)
    assert baseline["query_domain_accuracy"] == pytest.approx(2 / 3)
    m = results[0]
    assert (m.scored, m.correct, m.top2) == (3, 2, 3)
    assert m.confusions[("work-trade", "food")] == 1
    assert m.confident == 2 and m.confident_accuracy == 1.0

    report = format_report(baseline, results)
    assert "| m1 | true | 3/3 | 67% | 100% |" in report
    assert "work-trade -> food (1)" in report


# ===================
# FLAGS AND MIGRATION
# ===================


@pytest.mark.parametrize("flags", [None, {}, "true", {"sensitive": "true", "poor_quality": 1}])
def test_missing_or_non_boolean_flags_default_false(cfg, flags):
    data = json.loads(answer("food"))
    if flags is not None:
        data["flags"] = flags
    score = parse_reply(json.dumps(data), cfg.domain_keys)
    assert not any((score.sensitive, score.graphic_or_sexual, score.visible_text, score.poor_quality))


def test_not_relevant_and_flags_are_parsed_and_written(cfg, run_dir):
    reply = json.dumps({"domain": "not-relevant", "reason": "A blurry portrait",
                        "flags": {"sensitive": True, "graphic_or_sexual": False,
                                  "visible_text": True, "poor_quality": True}})
    score = parse_reply(reply, cfg.domain_keys)
    assert score.domain == "not-relevant" and score.sensitive and score.poor_quality
    run_scoring(FakeClient({("m1", "a1"): reply}), cfg, run_dir, ["m1"], candidate_ids={"a1"})
    row = read_scores(run_dir)[0]
    assert [row[k] for k in SCORE_FIELDS[-4:]] == ["true", "false", "true", "true"]


def test_old_header_migration_preserves_rows_and_resumes(cfg, run_dir):
    from src.prompt_generation.picture_scoring import migrate_score_header

    fields = SCORE_FIELDS[:-4]
    old_rows = [dict.fromkeys(fields, "") for _ in range(2)]
    old_rows[0].update(candidate_id="a1", model="m1", include_metadata="true", status="ok",
                       domain="food", reason='Old reason, with "quotes"\nand a newline')
    old_rows[1].update(candidate_id="b2", model="m1", include_metadata="true", status="error")
    path = run_dir / "domain-scores.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(old_rows)
    summary = run_scoring(FakeClient({("m1", "b2"): answer("food")}), cfg, run_dir, ["m1"],
                          candidate_ids={"a1", "b2"})
    assert summary["skipped_done"] == 1 and summary["ok"] == 1
    rows = read_scores(run_dir)
    assert len(rows) == 3 and list(rows[0]) == SCORE_FIELDS
    for before, after in zip(old_rows, rows):
        assert {key: after[key] for key in fields} == before
        assert all(after[key] == "" for key in SCORE_FIELDS[-4:])
    before = path.read_bytes()
    migrate_score_header(path)
    assert path.read_bytes() == before


def test_label_sheet_includes_not_relevant_after_unclear(cfg, run_dir):
    page = build_label_sheet(run_dir, cfg.domains).read_text()
    assert page.index('"key": "unclear"') < page.index('"key": "not-relevant"')
    assert "Not usable as a prompt for this project" in page


def test_flags_report_counts_latest_successful_scores_including_unlabelled(cfg, run_dir):
    write_labels(run_dir, {"a1": "food"})
    reply = json.dumps({"domain": "food", "flags": {"sensitive": True, "visible_text": True}})
    run_scoring(FakeClient({("m1", "a1"): reply, ("m1", "b2"): reply}), cfg, run_dir,
                ["m1"], candidate_ids={"a1", "b2"})
    # A repeated successful row should count once; errors should not add flags.
    row = read_scores(run_dir)[0]
    with (run_dir / "domain-scores.csv").open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCORE_FIELDS)
        writer.writerow(row)
        writer.writerow({**row, "status": "error", "flag_poor_quality": "true"})
    baseline, results = evaluate(run_dir)
    assert results[0].scored == 1 and results[0].flags["sensitive"] == 2
    report = format_report(baseline, results)
    assert "## Flags per model" in report
    assert "| m1 | true | 2 | 0 | 2 | 0 |" in report
