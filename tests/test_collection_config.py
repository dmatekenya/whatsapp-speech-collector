"""Recording targets in config/assignment_settings.yaml: per_participant x split."""

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.tasks.config import (
    DEFAULT_PATH,
    CollectionConfig,
    CollectionConfigError,
    load_collection_config,
    round_half,
)

TYPES = ("sentence", "image", "conversation_starter")


def settings(per_participant: float = 10, split: dict | None = None, **overrides: object) -> dict:
    values = {
        "task_types": {
            t: {"prompted": True, "speakers_per_prompt": 5, "speech_type": "scripted" if t == "sentence" else "spontaneous"}
            for t in TYPES
        },
        "session_size": 10,
        "assignment_expiry_hours": 24,
        "max_retakes_per_prompt": 2,
        "targets_minutes": {
            "per_participant": per_participant,
            "split": split if split is not None else {"sentence": 0.4, "image": 0.3, "conversation_starter": 0.3},
        },
        "clip_limits": {"min_seconds": 5, "default": {"flag_over": 30, "max": 30}},
    }
    values.update(overrides)
    return values


def config(per_participant: float = 10, split: dict | None = None) -> CollectionConfig:
    return CollectionConfig.model_validate(settings(per_participant, split))


# ===========
# SPLIT MATHS
# ===========
def test_per_type_targets_are_per_participant_times_split() -> None:
    cfg = config(10)
    assert cfg.default_targets == {"sentence": 4.0, "image": 3.0, "conversation_starter": 3.0}
    assert cfg.total(cfg.default_targets) == 10.0


@pytest.mark.parametrize(
    ("per_participant", "expected", "total"),
    [
        (7, {"sentence": 3.0, "image": 2.0, "conversation_starter": 2.0}, 7.0),  # 2.8, 2.1, 2.1
        (5, {"sentence": 2.0, "image": 1.5, "conversation_starter": 1.5}, 5.0),
        # 1.6, 1.2, 1.2: rounding can make the sum differ from per_participant;
        # the total shown is the sum.
        (4, {"sentence": 1.5, "image": 1.0, "conversation_starter": 1.0}, 3.5),
        (0, {"sentence": 0.0, "image": 0.0, "conversation_starter": 0.0}, 0.0),
    ],
)
def test_targets_round_to_half_minutes(per_participant: float, expected: dict, total: float) -> None:
    cfg = config(per_participant)
    assert cfg.default_targets == expected
    assert cfg.total(cfg.default_targets) == total


@pytest.mark.parametrize(
    ("minutes", "rounded"),
    [(1.24, 1.0), (1.25, 1.5), (1.5, 1.5), (1.74, 1.5), (2.75, 3.0), (0.2, 0.0), (0.3, 0.5), (3.0, 3.0)],
)
def test_round_half_rounds_halves_up(minutes: float, rounded: float) -> None:
    assert round_half(minutes) == rounded


def test_thirds_are_accepted_within_the_tolerance() -> None:
    cfg = config(30, {"sentence": 0.333, "image": 0.333, "conversation_starter": 0.334})
    assert cfg.default_targets == {"sentence": 10.0, "image": 10.0, "conversation_starter": 10.0}


# =============
# INVALID SPLIT
# =============
@pytest.mark.parametrize(
    ("split", "message"),
    [
        ({"sentence": 0.5, "image": 0.5}, "missing ['conversation_starter']"),
        (
            {"sentence": 0.4, "image": 0.3, "conversation_starter": 0.2, "video": 0.1},
            "not in task_types ['video']",
        ),
        ({"sentence": 0.4, "image": 0.3, "conversation_starter": 0.2}, "must add up to 1.0; it adds up to 0.9"),
        ({"sentence": 0.4, "image": 0.3, "conversation_starter": 0.302}, "must add up to 1.0; it adds up to 1.002"),
        ({"sentence": 0.8, "image": 0.5, "conversation_starter": -0.3}, "must not be negative: ['conversation_starter']"),
    ],
)
def test_invalid_split_is_refused(split: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message.replace("[", r"\[").replace("]", r"\]")):
        config(10, split)


def test_split_within_tolerance_is_accepted() -> None:
    config(10, {"sentence": 0.4, "image": 0.3, "conversation_starter": 0.3005})


def test_old_format_gets_a_clear_message() -> None:
    old = settings()
    old["targets_minutes"] = {"total": 10, "sentence": 4, "image": 3, "conversation_starter": 3}
    with pytest.raises(ValidationError, match="now takes per_participant"):
        CollectionConfig.model_validate(old)


def test_invalid_file_fails_to_load_with_path_and_reason(tmp_path: Path) -> None:
    path = tmp_path / "assignment_settings.yaml"
    path.write_text(yaml.safe_dump(settings(split={"sentence": 0.5, "image": 0.3, "conversation_starter": 0.3})))
    with pytest.raises(CollectionConfigError) as caught:
        load_collection_config(path)
    message = str(caught.value)
    assert message.startswith(f"{path}: targets_minutes: ")
    assert "must add up to 1.0; it adds up to 1.1" in message


def test_the_real_config_file_is_valid() -> None:
    cfg = load_collection_config(DEFAULT_PATH)
    assert set(cfg.targets_minutes.split) == set(cfg.task_types)


# =====================
# PARTICIPANT OVERRIDES
# =====================
def test_override_per_participant_uses_the_config_split() -> None:
    targets = config(10).targets_for({"targets_minutes": {"per_participant": 5}})
    assert targets == {"sentence": 2.0, "image": 1.5, "conversation_starter": 1.5}
    assert CollectionConfig.total(targets) == 5.0


def test_override_with_explicit_per_type_minutes() -> None:
    targets = config(10).targets_for({"targets_minutes": {"sentence": 2, "image": 1.25, "conversation_starter": 0}})
    assert targets == {"sentence": 2.0, "image": 1.25, "conversation_starter": 0.0}  # used as given
    assert CollectionConfig.total(targets) == 3.25  # total = their sum


def test_override_with_some_types_keeps_the_defaults_for_the_rest() -> None:
    assert config(10).targets_for({"targets_minutes": {"image": 9}}) == {
        "sentence": 4.0, "image": 9.0, "conversation_starter": 3.0,
    }  # fmt: skip


def test_per_type_minutes_win_over_per_participant() -> None:
    targets = config(10).targets_for({"targets_minutes": {"per_participant": 5, "sentence": 4}})
    assert targets == {"sentence": 4.0, "image": 1.5, "conversation_starter": 1.5}


@pytest.mark.parametrize(
    "attributes",
    [None, {}, {"targets_minutes": None}, {"targets_minutes": "ten"}, {"targets_minutes": {"total": 99, "video": 3}}],
)
def test_missing_or_unusable_overrides_use_the_defaults(attributes: dict | None) -> None:
    assert config(10).targets_for(attributes) == {"sentence": 4.0, "image": 3.0, "conversation_starter": 3.0}


# ==========
# TASK TYPES
# ==========
def with_types(task_types: dict, split: dict) -> dict:
    values = settings(split=split)
    values["task_types"] = task_types
    return values


FOUR = {
    "sentence": {"prompted": True, "speakers_per_prompt": 5, "speech_type": "scripted"},
    "image": {"prompted": True, "speakers_per_prompt": 7, "speech_type": "spontaneous"},
    "conversation_starter": {"prompted": True, "speakers_per_prompt": 3, "speech_type": "spontaneous"},
    "free_speech": {"prompted": False, "speech_type": "spontaneous"},
}
FOUR_SPLIT = {"sentence": 0.30, "image": 0.25, "conversation_starter": 0.25, "free_speech": 0.20}


def test_unprompted_type_without_speakers_is_valid() -> None:
    cfg = CollectionConfig.model_validate(with_types(FOUR, FOUR_SPLIT))
    assert list(cfg.task_types) == ["sentence", "image", "conversation_starter", "free_speech"]
    assert cfg.speakers_per_prompt == {"sentence": 5, "image": 7, "conversation_starter": 3}
    assert not cfg.is_prompted("free_speech") and cfg.is_prompted("image")
    cfg20 = cfg.split_targets(20)
    assert cfg20 == {"sentence": 6.0, "image": 5.0, "conversation_starter": 5.0, "free_speech": 4.0}


def test_unprompted_type_with_speakers_is_refused() -> None:
    types = {**FOUR, "free_speech": {"prompted": False, "speakers_per_prompt": 3, "speech_type": "spontaneous"}}
    with pytest.raises(ValidationError, match="speakers_per_prompt is only for prompted task types"):
        CollectionConfig.model_validate(with_types(types, FOUR_SPLIT))


def test_prompted_type_without_speakers_is_refused() -> None:
    types = {**FOUR, "image": {"prompted": True, "speech_type": "spontaneous"}}
    with pytest.raises(ValidationError, match="a prompted task type needs speakers_per_prompt"):
        CollectionConfig.model_validate(with_types(types, FOUR_SPLIT))


def test_split_must_include_the_unprompted_type() -> None:
    with pytest.raises(ValidationError, match=r"missing \['free_speech'\]"):
        CollectionConfig.model_validate(with_types(FOUR, {"sentence": 0.4, "image": 0.3, "conversation_starter": 0.3}))


def test_old_speakers_per_prompt_section_gets_a_clear_message(tmp_path: Path) -> None:
    old = settings()
    del old["task_types"]
    old["speakers_per_prompt"] = dict.fromkeys(TYPES, 5)
    path = tmp_path / "assignment_settings.yaml"
    path.write_text(yaml.safe_dump(old))
    with pytest.raises(CollectionConfigError, match="replace it with a task_types section"):
        load_collection_config(path)


# ============
# AUDIO ORIGIN
# ============
def test_audio_origin_rules() -> None:
    rules = CollectionConfig.model_validate(with_types(FOUR, FOUR_SPLIT)).audio_origin
    assert [rules.policy(o) for o in ("recorded", "forwarded", "uploaded", "unknown", None)] == [
        "accept", "reject", "reject", "accept", "accept",
    ]  # fmt: skip


def test_unknown_audio_origin_policy_is_refused() -> None:
    values = with_types(FOUR, FOUR_SPLIT)
    values["audio_origin"] = {"forwarded": "ignore"}
    with pytest.raises(ValidationError, match="audio_origin.forwarded"):
        CollectionConfig.model_validate(values)


def with_free_speech_override(override: dict) -> dict:
    types = {**FOUR, "free_speech": {**FOUR["free_speech"], "audio_origin": override}}
    return with_types(types, FOUR_SPLIT)


def test_task_type_audio_origin_override() -> None:
    cfg = CollectionConfig.model_validate(with_free_speech_override({"forwarded": "accept_flagged"}))
    assert cfg.audio_policy("forwarded", "free_speech") == "accept_flagged"
    # Origins it doesn't mention, other types and no task type: the global rules.
    assert cfg.audio_policy("uploaded", "free_speech") == "reject"
    assert cfg.audio_policy("recorded", "free_speech") == "accept"
    assert [cfg.audio_policy("forwarded", t) for t in ("sentence", "image", "conversation_starter", None)] == ["reject"] * 4


@pytest.mark.parametrize(
    ("override", "where"),
    [({"emailed": "accept"}, "task_types.free_speech.audio_origin.emailed"), ({"forwarded": "ignore"}, "task_types.free_speech.audio_origin.forwarded")],
)
def test_bad_task_type_audio_origin_override_is_refused(tmp_path: Path, override: dict, where: str) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(with_free_speech_override(override)))
    with pytest.raises(CollectionConfigError, match=where):
        load_collection_config(path)


def test_override_for_an_unknown_task_type_is_refused(tmp_path: Path) -> None:
    values = with_types({**FOUR, "free_speach": {**FOUR["free_speech"], "audio_origin": {"forwarded": "accept_flagged"}}}, FOUR_SPLIT)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(values))
    with pytest.raises(CollectionConfigError, match="free_speach"):
        load_collection_config(path)


def test_applying_the_rule_again_with_the_task_type_replaces_the_workers_rule() -> None:
    from src.storage.audio import apply_audio_origin
    from src.storage.models import Recording

    cfg = CollectionConfig.model_validate(
        with_free_speech_override({"forwarded": "accept_flagged", "uploaded": "accept"}) | {"audio_origin": {"uploaded": "accept_flagged"}}
    )
    recording = Recording(qc_status="pending", qc_reason=None)
    apply_audio_origin(recording, "forwarded", None, cfg)  # the worker: global rule
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")
    apply_audio_origin(recording, "forwarded", "free_speech", cfg)  # the engine, in free speech
    assert (recording.qc_status, recording.qc_reason) == ("needs_review", "speaker_check_pending")
    assert recording.audio_origin == "forwarded"

    uploaded = Recording(qc_status="pending", qc_reason=None)
    apply_audio_origin(uploaded, "uploaded", None, cfg)
    apply_audio_origin(uploaded, "uploaded", "free_speech", cfg)  # accept: back to unchecked
    assert (uploaded.qc_status, uploaded.qc_reason) == ("pending", None)


def test_the_real_config_has_the_four_types_and_rules() -> None:
    cfg = load_collection_config(DEFAULT_PATH)
    assert cfg.default_targets == {"sentence": 6.0, "image": 5.0, "conversation_starter": 5.0, "free_speech": 4.0}
    limits = cfg.clip_limits
    assert limits.min_seconds == 5
    assert {t: limits.max_seconds(t, "recorded") for t in (*cfg.task_types, None)} == {
        "sentence": 45, "image": 60, "conversation_starter": 60, "free_speech": 60, None: 60,
    }  # fmt: skip
    assert limits.max_seconds("free_speech", "forwarded") == limits.max_seconds("free_speech", "uploaded") == 120
    assert limits.max_seconds("sentence", "forwarded") == 45  # only free speech has a higher max
    assert [limits.length_flag(31, t) for t in (*cfg.task_types, None)] == [
        "long_sentence", "long_clip", "long_clip", "long_clip", "long_clip",
    ]  # fmt: skip
    assert limits.length_flag(30, "sentence") is None
    assert (cfg.clip_checks.long_clip_seconds, cfg.clip_checks.long_clip_min_active_ratio) == (30, 0.20)
    assert cfg.audio_origin.forwarded == "reject" and cfg.audio_origin.uploaded == "reject"
    # Forwarded voice notes and audio files count (flagged) in free speech only.
    for origin in ("forwarded", "uploaded"):
        assert cfg.audio_policy(origin, "free_speech") == "accept_flagged"
        assert [cfg.audio_policy(origin, t) for t in ("sentence", "image", "conversation_starter", None)] == ["reject"] * 4


# ===========
# SPEECH TYPE
# ===========
def test_speech_type_groups_task_types() -> None:
    cfg = CollectionConfig.model_validate(with_types(FOUR, FOUR_SPLIT))
    assert [cfg.speech_type(t) for t in cfg.task_types] == ["scripted", "spontaneous", "spontaneous", "spontaneous"]
    # 20 minutes split 30/25/25/20: 30% scripted, 70% spontaneous.
    assert cfg.by_speech_type(cfg.split_targets(20)) == {"scripted": 6.0, "spontaneous": 14.0}
    assert cfg.by_speech_type({"sentence": 1.5, "free_speech": 0.5, "video": 9}) == {"scripted": 1.5, "spontaneous": 0.5}


@pytest.mark.parametrize("speech_type", ["read", "Scripted", "", None])
def test_unknown_speech_type_is_refused(speech_type: object) -> None:
    types = {**FOUR, "image": {"prompted": True, "speakers_per_prompt": 7, "speech_type": speech_type}}
    with pytest.raises(ValidationError, match="task_types.image.speech_type"):
        CollectionConfig.model_validate(with_types(types, FOUR_SPLIT))


def test_speech_type_is_required() -> None:
    types = {**FOUR, "free_speech": {"prompted": False}}
    with pytest.raises(ValidationError, match="task_types.free_speech.speech_type"):
        CollectionConfig.model_validate(with_types(types, FOUR_SPLIT))


def test_the_real_config_is_30_percent_scripted() -> None:
    cfg = load_collection_config(DEFAULT_PATH)
    assert cfg.by_speech_type(cfg.default_targets) == {"scripted": 6.0, "spontaneous": 14.0}


# ===========
# CLIP LIMITS
# ===========
LIMITS = {
    "min_seconds": 5,
    "default": {"flag_over": 30, "max": 60},
    "task_types": {
        "sentence": {"flag_over": 30, "max": 45, "flag": "long_sentence"},
        "image": {"flag_over": 30, "max": 60},
    },
}


def with_limits(**changes: object) -> dict:
    limits = yaml.safe_load(yaml.safe_dump(LIMITS))  # a deep copy
    for path, value in changes.items():
        *parents, last = path.split("__")
        target = limits
        for part in parents:
            target = target[part]
        target[last] = value
    return settings(clip_limits=limits)


def test_clip_limits_per_type_with_default_for_the_rest() -> None:
    limits = CollectionConfig.model_validate(with_limits()).clip_limits
    assert limits.max_seconds("sentence", "recorded") == 45
    assert limits.max_seconds("conversation_starter", "recorded") == 60  # not listed: default
    assert limits.max_seconds(None, "forwarded") == 60
    assert limits.length_flag(46, "sentence") == "long_sentence"
    assert limits.length_flag(31, "conversation_starter") == "long_clip"
    assert limits.length_flag(None, "sentence") is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"min_seconds": 0}, "clip_limits.min_seconds"),
        ({"default__max": -1}, "clip_limits.default.max"),
        ({"min_seconds": 30}, "default: min_seconds must be below flag_over"),
        ({"task_types__sentence__flag_over": 50}, "flag_over must not exceed max"),
        ({"task_types__image__max_forwarded_or_uploaded": 50}, "max_forwarded_or_uploaded must not be below max"),
        ({"task_types__sentence__flag": "very_long"}, "clip_limits.task_types.sentence.flag"),
        ({"task_types__sentence__maximum": 45}, "clip_limits.task_types.sentence.maximum"),
        ({"task_types__video": {"flag_over": 30, "max": 60}}, "clip_limits.task_types: not in task_types ['video']"),
        ({"default__flag": "long_sentence"}, "default: flag must be long_clip"),
    ],
)
def test_bad_clip_limits_are_refused(tmp_path: Path, changes: dict, message: str) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(with_limits(**changes)))
    with pytest.raises(CollectionConfigError, match=re.escape(message)):
        load_collection_config(path)


def test_old_clip_seconds_gets_a_clear_message() -> None:
    old = settings()
    del old["clip_limits"]
    old["clip_seconds"] = {"min": 5, "max": 29}
    with pytest.raises(ValidationError, match="clip_seconds is replaced by clip_limits"):
        CollectionConfig.model_validate(old)


@pytest.mark.parametrize(
    ("active", "duration", "expected"),
    [
        (1.4, 10, "no_speech"),  # under min_active_seconds
        (1.6, 10, "low_speech"),  # 16% of a short clip: accepted, flagged
        (5.9, 30, "low_speech"),  # 30 s is not over long_clip_seconds
        (6.1, 31, "no_speech"),  # long and under 20% active
        (23, 119, "no_speech"),
        (7, 31, None),  # long, 23% active
        (25, 120, None),
    ],
)
def test_long_mostly_silent_clips_are_no_speech(active: float, duration: float, expected: str | None) -> None:
    checks = CollectionConfig.model_validate(settings()).clip_checks
    assert checks.problem(active, duration) == expected
