import json
from collections import Counter
from dataclasses import replace

import pytest
import yaml

from scripts.prompt_generation import generate_starters
from src.config import PROJECT_ROOT
from src.prompt_generation.text.domains import read_kept
from src.prompt_generation.text.sentences import dedupe_key, sentence_id
from src.prompt_generation.text.starters import (
    ANY_TOPIC,
    Duplicates,
    MockClient,
    TopicProgress,
    _filter_reason,
    build_messages,
    evaluate_reply,
    load_settings,
    quotas,
    run_generation,
)

GOOD = [
    "Tiuzeni za fungo labwino limene limakukumbutsani za kwanu.",
    "Ndi phokoso liti limene mumamva mukadzuka m'mawa kwambiri?",
    "Kumbukirani munthu amene anakuphunzitsani kuimba nyimbo yachikhalidwe.",
]
KODI = "Kodi ndi malo ati amene mumapitako kukapuma?"


def reply(*texts, cost=0):
    return {
        "choices": [{"message": {"content": json.dumps([{"text": text} for text in texts])}}],
        "usage": {"cost": cost},
    }


@pytest.fixture
def settings():
    return load_settings()


def test_quotas_match_manual_and_round_for_pilot(settings):
    spec = yaml.safe_load((PROJECT_ROOT / "config/sentence-extraction.yaml").read_text())
    assert {domain: share * 90 for domain, share in settings.shares.items()} == spec["domain_targets"]
    for total in (7000, 150, 7):
        split = quotas(settings, total)
        assert sum(split.values()) == total
        for domain in settings.shares:
            counts = [split[domain, topic] for topic in settings.topics[domain]]
            any_count = split[domain, ANY_TOPIC]
            domain_total = sum(counts) + any_count
            assert any_count == (domain_total + 5) // 10
            assert max(counts) - min(counts) <= 1
            if total == 7000:
                assert domain_total == settings.shares[domain] * 70
            if total == 150:
                assert domain_total > 0
    assert sum(n for (domain, _), n in quotas(settings, 7).items() if domain == "family") == 1


@pytest.mark.parametrize(("text", "reason"), [
    ("Tiuzeni.", "word_length"),
    ("Tiuzeni " * 16, "word_length"),
    ("Tiuzeni za zinthu ٣ zimene mumanyamula paulendo.", "digits"),
    ("Fotokozani zimene BOMA, lingachite kuti anthu azisangalala.", "banned_phrase"),
    ("Kodi mumakonda kuonera mbalame zikudya munda mwanu?", "yes_no"),
    ("Mumakonda kuonera mbalame zikudya munda mwanu?", "yes_no"),
    ("Kodi mumakonda kuonera mbalame? Tiuzeni zimene mumakonda kuziona.", ""),
    ("Kodi nyengo ya mvula imasintha bwanji zochita zanu?", ""),
])
def test_filters(settings, text, reason):
    assert _filter_reason(text, dedupe_key(text), settings.banned_phrases) == reason


def test_kodi_caps(settings):
    state = TopicProgress([])
    accepted, rejected, _ = evaluate_reply(
        reply(KODI, *GOOD), settings, "family", "elders", 4, state, Counter(), Duplicates(), "mock",
    )
    assert len(accepted) == 4
    assert not rejected
    for progress, domain_counts in [
        (TopicProgress(GOOD[:2]), Counter({("family", "total"): 10})),
        (TopicProgress(GOOD), Counter({("family", "total"): 3, ("family", "kodi"): 1})),
    ]:
        accepted, rejected, _ = evaluate_reply(
            reply(KODI), settings, "family", "elders", 1, progress, domain_counts, Duplicates(), "mock",
        )
        assert not accepted
        assert [row["reason"] for row in rejected] == ["kodi_cap"]


def test_dedupe_is_global_and_checks_normalised_and_near_matches(settings):
    duplicates = Duplicates()
    args = (settings, "family", "elders", 1, TopicProgress([]), Counter(), duplicates, "mock")
    assert evaluate_reply(reply(GOOD[0]), *args)[0]
    for variant in (GOOD[0].upper(), GOOD[0].replace("labwino", "losangalatsa")):
        accepted, rejected, _ = evaluate_reply(
            reply(variant), settings, "food", "cooking", 1, TopicProgress([]), Counter(), duplicates, "mock",
        )
        assert not accepted
        assert rejected[0]["reason"] == "duplicate"


def small_settings(settings, **overrides):
    return replace(settings, **{
        "shares": {"family": 1}, "topics": {"family": {"elders": settings.topics["family"]["elders"]}},
        "total": 3, "per_call": 2, **overrides,
    })


class ScriptedClient:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def chat(self, model, messages):
        self.requests.append(messages[-1]["content"])
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return result


def test_resume_preserves_dedupe_and_requests_only_remaining_quota(tmp_path, settings):
    settings = small_settings(settings)
    first = ScriptedClient(reply(*GOOD[:2]), RuntimeError("interrupted"))
    with pytest.raises(RuntimeError, match="interrupted"):
        run_generation(tmp_path, settings, client=first)
    assert len(read_kept(tmp_path / "starters.csv")) == 2
    assert first.requests[0].startswith("Write exactly 2 starters.")
    assert first.requests[1].startswith("Write exactly 1 starters.")

    resumed = ScriptedClient(reply(GOOD[0]), reply(GOOD[2]))
    run_generation(tmp_path, settings, client=resumed)
    assert len(resumed.requests) == 2
    assert all(request.startswith("Write exactly 1 starters.") for request in resumed.requests)
    assert GOOD[1] in resumed.requests[0]
    assert read_kept(tmp_path / "rejected.csv")[0]["reason"] == "duplicate"
    assert len(read_kept(tmp_path / "starters.csv")) == 3
    assert read_kept(tmp_path / "shortfalls.csv")[0]["shortfall"] == "0"

    complete = ScriptedClient()
    run_generation(tmp_path, settings, client=complete)
    assert not complete.requests


@pytest.mark.parametrize(("limit", "calls", "reason"), [(None, 5, "empty_calls"), (2, 2, "call_limit")])
def test_stopping_persists_shortfalls_without_reallocating(tmp_path, settings, limit, calls, reason):
    settings = small_settings(settings, total=10, max_calls_per_topic=limit)
    client = ScriptedClient(*[reply() for _ in range(2 * calls if limit else calls + 2)])
    run_generation(tmp_path, settings, client=client)
    by_topic = {row["topic"]: row for row in read_kept(tmp_path / "shortfalls.csv")}
    assert by_topic["elders"]["calls"] == str(calls)
    assert by_topic["elders"]["reason"] == reason
    assert by_topic["elders"]["shortfall"] == "9"
    assert by_topic[ANY_TOPIC]["quota"] == by_topic[ANY_TOPIC]["shortfall"] == "1"


def test_consecutive_empty_counter_resets_after_acceptance(tmp_path, settings):
    settings = small_settings(settings, max_calls_per_topic=12)
    client = ScriptedClient(*([reply()] * 4), reply(GOOD[0]), *([reply()] * 5))
    run_generation(tmp_path, settings, client=client)
    row = read_kept(tmp_path / "shortfalls.csv")[0]
    assert (row["calls"], row["accepted"], row["reason"]) == ("10", "1", "empty_calls")


def test_run_cap_pauses_and_can_resume(tmp_path, settings):
    settings = small_settings(settings)
    first = ScriptedClient(reply(*GOOD[:2], cost=settings.scoring.max_spend_usd))
    run_generation(tmp_path, settings, client=first)
    assert read_kept(tmp_path / "shortfalls.csv")[0]["reason"] == "pending"
    run_generation(tmp_path, settings, client=ScriptedClient(reply(GOOD[2])))
    assert len(read_kept(tmp_path / "starters.csv")) == 3


def test_prompt_uses_draft_topic_and_last_40_examples(settings):
    history = [{"text": f"Earlier starter {i:02d}"} for i in range(50)]
    system, user = build_messages(settings, "family", "elders", 2, history)
    examples = yaml.safe_load(generate_starters.DEFAULT_CONFIG.read_text())["examples"]
    assert len(examples) == 12
    assert all(example in system["content"] for example in examples.values())
    assert settings.topics["family"]["elders"]["covers"] in user["content"]
    assert "Do not repeat or paraphrase" in user["content"]
    assert "Earlier starter 09" not in user["content"]
    assert user["content"].count("Earlier starter") == 40


def test_mock_cli(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        pytest.fail("mock contacted OpenRouter")

    monkeypatch.setattr("src.prompt_generation.text.starters.OpenRouterClient", fail)
    monkeypatch.setattr(generate_starters, "OUTPUT_ROOT", tmp_path)
    assert generate_starters.main(["--run-id", "full", "--mock"]) == 0
    report = capsys.readouterr().out
    assert report.startswith("Estimated cost")
    assert "spent $0.0000" in report
    accepted = read_kept(tmp_path / "full/starters.csv")
    assert accepted
    assert all(row["model"] == "mock" and row["source_category"] == "ai-generated" for row in accepted)
    assert all(row["candidate_id"] == sentence_id(dedupe_key(row["text"])) for row in accepted)
    rejected = read_kept(tmp_path / "full/rejected.csv")
    assert {"duplicate", "banned_phrase", "word_length", "kodi_cap", "yes_no"} <= {
        row["reason"] for row in rejected
    }
    assert any(len(text.split()) > 15 for text in MockClient.items)
    assert generate_starters.main(["--run-id", "pilot", "--pilot", "150", "--mock"]) == 0
    pilot_report = capsys.readouterr().out
    assert all(f"  {domain}:" in pilot_report for domain in load_settings().shares)
    assert "generated," in pilot_report and "% Kodi" in pilot_report
