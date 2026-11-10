"""src/qc/text.py must normalise exactly like the training normaliser's copy in
scripts/modal_asr_compare.py: checked against pairs generated from that copy (the script
itself isn't imported, as it loads Modal and reads HF_TOKEN)."""

import json
from pathlib import Path

import pytest

from src.qc.text import normalize_transcript

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "normalize_transcript_pairs.json").read_text())


@pytest.mark.parametrize("pair", FIXTURE["pairs"], ids=lambda pair: repr(pair["input"][:30]))
def test_same_output_as_the_compare_scripts_copy(pair: dict) -> None:
    assert normalize_transcript(pair["input"]) == pair["output"]


def test_the_fixture_is_varied() -> None:
    inputs = " ".join(pair["input"] for pair in FIXTURE["pairs"])
    for feature in ("MONI", "?", "’", "‘", "`", "´", "ʼ", "  ", "\n", "[", "(", "5,000", "download", "“"):
        assert feature in inputs, feature
