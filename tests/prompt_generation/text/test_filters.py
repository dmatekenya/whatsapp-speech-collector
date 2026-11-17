"""Stage 2 filters: one case per rule, using the real sentence config."""

from __future__ import annotations

import pytest

from src.prompt_generation.text.filters import drop_reason, is_political, political_patterns
from src.prompt_generation.text.sentences import load_sentence_settings

SETTINGS = load_sentence_settings()
RULES = SETTINGS.filters


def reason(text: str, *, ocr: bool = False, unknown: list[str] | None = None) -> str | None:
    padding = " ".join(["mawu"] * max(0, 15 - len(text.split())))
    if padding:
        text = f"{text.split()[0]} {padding} {' '.join(text.split()[1:])}"
    return drop_reason(text, RULES, ocr=ocr, unknown=unknown or [])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mwana wake akupita kusukulu m'mawa uno.", None),
        ("mwana wake akupita kusukulu m'mawa uno.", "bad_start"),
        ("Mwana wake akupita kusukulu m'mawa uno", "bad_end"),
        ("Mwana " + "wake " * 54 + "lero.", "too_long"),
        ("Mwana wake ali ndi zaka 5 lero.", "has_digit"),
        ("Ichi ndi (i) chitsanzo lero.", "list_or_junk"),
        ("Ichi ndi chitsanzo... lero.", "list_or_junk"),
        ("Mwana (wake) akupita lero.", "has_brackets"),
        ("Mwana akuti “moni lero.", "unbalanced"),
        ("The boy is going to the school in the morning and he is very happy.", "english"),
        ("Mwana wake akupita kusukulu lero%.", "importer_check"),
    ],
)
def test_filter_reasons(text: str, expected: str | None) -> None:
    assert reason(text) == expected


def test_ocr_noise_and_unknown_words_apply_only_to_ocr() -> None:
    noisy = "Mwana wake akupita ku“sukulu” lero."
    assert reason(noisy) is None
    assert reason(noisy, ocr=True) == "ocr_noise"
    unknown_text = "Zaka khumi zapitazo lero."
    assert reason(unknown_text, unknown=["zaka", "khumi", "zapitazo"]) is None
    assert reason(unknown_text, ocr=True, unknown=["zaka", "khumi", "zapitazo"]) == "ocr_unknown_words"


@pytest.mark.parametrize("words, expected", [(12, "too_short"), (15, None), (55, None), (56, "too_long")])
def test_unit_length_boundaries(words: int, expected: str | None) -> None:
    text = "Mwana " + "wake " * (words - 2) + "lero."
    assert drop_reason(text, RULES, ocr=False, unknown=[]) == expected


@pytest.mark.parametrize("punctuation", [".", "?", "!"])
def test_lowercase_continuation_after_punctuation_is_dropped(punctuation: str) -> None:
    assert reason(f"Izi ndi zoona weniweni{punctuation} koma anthu akudziwa bwino.") == "mid_sentence_break"


@pytest.mark.parametrize("punctuation", [".", ",", "?", "!"])
def test_closing_quote_after_punctuation_is_not_ocr_noise(punctuation: str) -> None:
    assert reason(f'Mwana anati “Ndabwera{punctuation}” Ndipo anthu onse akondwera.', ocr=True) is None


def test_lowercase_uppercase_word_is_ocr_noise_only() -> None:
    text = "Mwana wake zZimachitika lero."
    assert reason(text, ocr=True) == "ocr_noise"
    assert reason(text) is None


def test_political_keywords_respect_case_for_all_caps() -> None:
    patterns = political_patterns(SETTINGS.political_keywords)
    assert is_political("Chipani chathu chapambana lero.", patterns)
    assert is_political("Mutharika ananena lero.", patterns)
    assert not is_political("Mwana wa pp akupita lero.", patterns)
