"""Tier-2 metrics and levels: pure functions, no database."""

import math
import wave
from pathlib import Path

import numpy as np
import pytest
import yaml

from src.qc import metrics as m
from src.qc.levels import (
    ALL_OK,
    NOT_APPLICABLE,
    QcConfigError,
    clip_level,
    load_qc_config,
    metric_levels,
)

CONFIG = load_qc_config()


def _all_metrics_on():
    """The real cut-offs with every metric setting a level, for every task type: tests the
    weakest-link machinery that a metric switched back on (sets_level: true) would use."""
    config = CONFIG.model_copy(deep=True)
    for rules in config.levels.values():
        rules.sets_level = True
    config.word_count.sets_level = True
    config.levelled_task_types = ["sentence", "image", "conversation_starter", "free_speech"]
    return config


ALL_ON = _all_metrics_on()
SR = 16000


# =======
# METRICS
# =======
@pytest.mark.parametrize(
    ("prompt", "transcript", "cer", "wer"),
    [
        ("Moni, muli bwanji?", "moni muli bwanji", 0.0, 0.0),  # normalised: case and punctuation don't count
        ("abcd", "abed", 0.25, 1.0),
        ("moni muli bwanji", "moni bwanji", 5 / 16, 1 / 3),
        ("Kodi n’chifukwa chiyani?", "kodi n'chifukwa chiyani", 0.0, 0.0),  # apostrophe variants
        ("moni muli", "", 1.0, 1.0),  # nothing recognised
    ],
)
def test_cer_and_wer_on_known_pairs(prompt: str, transcript: str, cer: float, wer: float) -> None:
    assert m.cer(prompt, transcript) == pytest.approx(cer)
    assert m.wer(prompt, transcript) == pytest.approx(wer)


def test_no_cer_without_a_prompt_text() -> None:
    assert m.cer("", "moni") is None and m.wer("?!", "moni") is None and m.length_ratio(None, "moni") is None


def test_length_ratio() -> None:
    assert m.length_ratio("Moni muli bwanji", "moni muli") == pytest.approx(9 / 16)
    assert m.length_ratio("moni", "moni moni") == pytest.approx(9 / 4)


def test_compression_ratio_flags_repetition() -> None:
    normal = "Kodi n'chifukwa chiyani anthu ambiri amaona kuti maphunziro ndi ofunika kwambiri pa moyo"
    repeated = " ".join(["ndikufuna kupita kumsika"] * 12)
    assert m.compression_ratio(normal) < 2.0
    assert m.compression_ratio(repeated) > 2.4
    assert m.compression_ratio("") is None and m.compression_ratio("   ") is None


def test_has_digits() -> None:
    assert m.has_digits("Anagula matumba 5 a chimanga") is True
    assert m.has_digits("Anagula matumba asanu a chimanga") is False
    assert m.has_digits(None) is False


def test_word_count_and_words_per_second() -> None:
    assert m.word_count("Moni, muli bwanji? (inaudible)") == 3
    assert m.word_count("") == 0 and m.word_count(None) == 0
    assert m.words_per_second(6, 3.0) == 2.0
    assert m.words_per_second(6, None) is None and m.words_per_second(6, 0.0) is None


def test_speech_ratio() -> None:
    assert m.speech_ratio(3.0, 10.0) == pytest.approx(0.3)
    assert m.speech_ratio(None, 10.0) is None and m.speech_ratio(3.0, 0.0) is None and m.speech_ratio(3.0, None) is None


def _tone(seconds: float = 3.0) -> np.ndarray:
    """A 220 Hz tone switched on and off (syllables and pauses)."""
    t = np.arange(int(SR * seconds)) / SR
    envelope = (np.sin(2 * np.pi * 3 * t) > 0).astype(float) * np.abs(np.sin(2 * np.pi * 3 * t))
    return 0.5 * envelope * np.sin(2 * np.pi * 220 * t)


def _speech_like(seconds: float = 3.0, seed: int = 4) -> np.ndarray:
    """Amplitudes distributed as WADA-SNR models clean speech (gamma, shape 0.4), peak 0.9."""
    rng = np.random.default_rng(seed)
    x = rng.gamma(0.4, 1.0, int(SR * seconds)) * rng.choice([-1.0, 1.0], int(SR * seconds))
    return 0.9 * x / np.abs(x).max()


@pytest.mark.parametrize("snr", [0, 5, 10, 15, 20, 30])
def test_wada_snr_recovers_a_known_snr(snr: int) -> None:
    rng = np.random.default_rng(snr)
    speech = rng.gamma(0.4, 1.0, SR * 10) * rng.choice([-1.0, 1.0], SR * 10)
    noise = rng.normal(0, np.sqrt(np.mean(speech**2) / 10 ** (snr / 10)), speech.size)
    assert m.wada_snr(speech + noise) == pytest.approx(snr, abs=1.0)


@pytest.mark.parametrize("make", [_tone, _speech_like])
def test_wada_snr_is_lower_with_noise(make) -> None:
    rng = np.random.default_rng(1)
    clean = make()
    noisy = clean + rng.normal(0, 0.05, clean.size)
    very_noisy = clean + rng.normal(0, 0.2, clean.size)
    snr_clean, snr_noisy, snr_very = m.wada_snr(clean), m.wada_snr(noisy), m.wada_snr(very_noisy)
    assert snr_clean > snr_noisy > snr_very
    assert snr_very < 10  # in the "under 10 dB" range
    assert -20 <= snr_very and snr_clean <= 100


def test_wada_snr_edges() -> None:
    assert m.wada_snr(np.zeros(1000)) is None and m.wada_snr(np.array([])) is None
    noise = np.random.default_rng(2).normal(0, 1, SR * 5)
    assert m.wada_snr(noise) < 0  # pure noise: very low
    table = m._WADA_G
    assert len(table) == len(m._WADA_DB) == 121 and bool(np.all(np.diff(table) > 0))
    # The table's ends: pure Gaussian noise, and the gamma-speech limit log(0.4) - digamma(0.4).
    assert table[0] == pytest.approx(0.5 * math.log(2 / math.pi) + (0.5772156649 + math.log(2)) / 2, abs=0.002)
    assert table[-1] < 1.6451


def test_read_wav(tmp_path: Path) -> None:
    samples = (_tone(1.0) * 32767).astype("<i2")
    path = tmp_path / "clip.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(samples.tobytes())
    data = m.read_wav(path)
    assert data.size == SR and np.allclose(data, samples)
    assert m.wada_snr(data) == pytest.approx(m.wada_snr(samples.astype(float)))


# ======
# LEVELS
# ======
@pytest.mark.parametrize(
    ("metric", "value", "level"),
    [
        ("cer", 0.0, 4), ("cer", 0.10, 4), ("cer", 0.1001, 3), ("cer", 0.15, 3), ("cer", 0.1501, 2),
        ("cer", 0.40, 2), ("cer", 0.4001, 1), ("cer", 1.3, 1),
        ("compression_ratio", 1.2, 4), ("compression_ratio", 2.0, 4), ("compression_ratio", 2.01, 3),
        ("compression_ratio", 2.4, 3), ("compression_ratio", 2.41, 1),
        ("words_per_second", 0.8, 4), ("words_per_second", 3.5, 4), ("words_per_second", 0.79, 3),
        ("words_per_second", 0.5, 3), ("words_per_second", 3.51, 3), ("words_per_second", 4.5, 3),
        ("words_per_second", 0.49, 2), ("words_per_second", 4.51, 2), ("words_per_second", 0.0, 2),
        ("avg_logprob", -0.2, 4), ("avg_logprob", -0.5, 4), ("avg_logprob", -0.51, 3), ("avg_logprob", -1.0, 3),
        ("avg_logprob", -1.01, 2),
        ("snr_db", 35.0, 4), ("snr_db", 20.0, 4), ("snr_db", 19.9, 3), ("snr_db", 10.0, 3), ("snr_db", 9.9, 2),
        ("speech_ratio", 0.4, 4), ("speech_ratio", 0.39, 3), ("speech_ratio", 0.2, 3), ("speech_ratio", 0.19, 2),
        ("mean_dbfs", -20.0, 4), ("mean_dbfs", -35.0, 4), ("mean_dbfs", -35.1, 3), ("mean_dbfs", -40.0, 3),
        ("mean_dbfs", -40.1, 2),
    ],
)  # fmt: skip
def test_each_cut_off(metric: str, value: float, level: int) -> None:
    # The bands are kept for report-only metrics too, so they can be switched back on.
    assert CONFIG.levels[metric].level(value) == level
    assert metric_levels({metric: value}, "sentence", ALL_ON) == {metric: level}


@pytest.mark.parametrize(
    ("metric", "value"),
    [("compression_ratio", 3.0), ("words_per_second", 0.1), ("avg_logprob", -2.0), ("snr_db", 0.0),
     ("speech_ratio", 0.05), ("mean_dbfs", -60.0), ("word_count", 0)],
)  # fmt: skip
def test_only_cer_sets_a_level_since_2026_11_11(metric: str, value: float) -> None:
    assert metric_levels({metric: value}, "sentence", CONFIG) == {}
    assert clip_level({metric: value, "cer": 0.05}, "sentence", CONFIG) == (4, ALL_OK)


@pytest.mark.parametrize(
    ("cer", "level"), [(0.0, 4), (0.10, 4), (0.1001, 3), (0.15, 3), (0.1501, 2), (0.40, 2), (0.4001, 1), (1.3, 1)]
)
def test_sentence_levels_come_from_cer(cer: float, level: int) -> None:
    assert clip_level({"cer": cer, "snr_db": 0.0, "word_count": 9}, "sentence", CONFIG)[0] == level


@pytest.mark.parametrize("task_type", ["image", "conversation_starter", "free_speech"])
def test_spontaneous_types_get_no_level(task_type: str) -> None:
    metrics = {"cer": 0.9, "word_count": 0, "compression_ratio": 5.0, "snr_db": 0.0}
    assert clip_level(metrics, task_type, CONFIG) == (None, NOT_APPLICABLE)


@pytest.mark.parametrize(
    ("task_type", "words", "expected"),
    [
        ("sentence", 0, {"word_count": 1}),
        ("image", 0, {"word_count": 1}),
        ("free_speech", 0, {"word_count": 1}),
        ("image", 4, {"word_count": 2}),
        ("conversation_starter", 4, {"word_count": 2}),
        ("free_speech", 4, {"word_count": 2}),
        ("image", 5, {}),
        ("sentence", 3, {}),  # the short rule is for the spontaneous types only
    ],
)
def test_word_count_rules(task_type: str, words: int, expected: dict) -> None:
    assert metric_levels({"word_count": words}, task_type, ALL_ON) == expected


def test_weakest_link_and_reasons() -> None:
    good = {"cer": 0.05, "compression_ratio": 1.5, "words_per_second": 2.0, "avg_logprob": -0.3,
            "snr_db": 25.0, "speech_ratio": 0.6, "mean_dbfs": -25.0, "word_count": 10, "has_digits": False}  # fmt: skip
    assert clip_level(good, "sentence", ALL_ON) == (4, ALL_OK)
    assert clip_level({**good, "snr_db": 15.0}, "sentence", ALL_ON) == (3, "snr_db:3")
    assert clip_level({**good, "snr_db": 15.0, "cer": 0.3}, "sentence", ALL_ON) == (2, "cer:2")
    # Ties: every metric at the lowest level, sorted by name.
    assert clip_level({**good, "compression_ratio": 3.0, "word_count": 0}, "image", ALL_ON) == (
        1, "compression_ratio:1,word_count:1",
    )  # fmt: skip
    assert clip_level({**good, "speech_ratio": 0.1, "mean_dbfs": -45.0}, "image", ALL_ON) == (
        2, "mean_dbfs:2,speech_ratio:2",
    )  # fmt: skip


def test_digits_cap_sentences_at_2() -> None:
    clean = {"cer": 0.0, "word_count": 8, "has_digits": True}
    assert clip_level(clean, "sentence", CONFIG) == (2, "has_digits:2")
    assert clip_level({**clean, "cer": 0.3}, "sentence", CONFIG) == (2, "cer:2,has_digits:2")
    assert clip_level({**clean, "cer": 0.9}, "sentence", CONFIG) == (1, "cer:1")
    assert clip_level({"cer": 0.0, "word_count": 8, "has_digits": False}, "sentence", CONFIG) == (4, ALL_OK)


def test_skipped_metrics() -> None:
    # CER outside sentences; a missing avg_logprob (long-form clip); wer and length_ratio never level.
    metrics = {"cer": 0.9, "avg_logprob": None, "wer": 5.0, "length_ratio": 0.1, "word_count": 20}
    assert metric_levels(metrics, "image", ALL_ON) == {}
    assert clip_level({**metrics, "snr_db": 30.0}, "free_speech", ALL_ON) == (4, ALL_OK)
    # A sentence with no level-setting metric (no prompt text, so no CER): no level either.
    assert clip_level({"wer": 0.5, "snr_db": 30.0}, "sentence", CONFIG) == (None, NOT_APPLICABLE)


# ======
# CONFIG
# ======
def test_the_config_matches_the_methodology_versions() -> None:
    assert (CONFIG.metrics_version, CONFIG.thresholds_version, CONFIG.model) == ("1", "2026-11-11-cer-only", "whisper_b")
    assert CONFIG.levelled_task_types == ["sentence"]
    assert [name for name, rules in CONFIG.levels.items() if rules.sets_level] == ["cer"]
    assert CONFIG.has_digits.sets_level and not CONFIG.word_count.sets_level


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c["levels"]["cer"]["bands"].reverse(), "highest level down"),
        (lambda c: c["levels"]["cer"].update(otherwise=2), "otherwise"),
        (lambda c: c["levels"]["cer"]["bands"][0].update(level=5), "less than or equal to 4"),
        (lambda c: c["levels"].update(clipping={"bands": [{"level": 4, "max": 1}], "otherwise": 1}), "unknown metric"),
        (lambda c: c["word_count"].update(short_applies_to=["pictures"]), "not in task_types"),
        (lambda c: c.update(levelled_task_types=["sentences"]), "not in task_types"),
        (lambda c: c.update(metrics_version="2"), "metrics_version"),
        (lambda c: c["levels"]["snr_db"]["bands"][0].update(min=30, max=20), "above max"),
    ],
)
def test_bad_config_is_refused_clearly(tmp_path: Path, change, message: str) -> None:
    config = yaml.safe_load(Path("config/qc_tier2.yaml").read_text())
    change(config)
    path = tmp_path / "qc_tier2.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(QcConfigError, match=message):
        load_qc_config(path)
