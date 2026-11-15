"""The training normaliser, for CER/WER and word counts.

A copy of the training repo's src/data_utils/text_utils.py::normalize_transcript (default
settings), the same copy as in scripts/modal_asr_compare.py. Keep the two identical:
tests/test_qc_text.py checks this one against input/output pairs generated from the compare
script's copy (tests/fixtures/normalize_transcript_pairs.json).
"""

import re
import unicodedata

_APOS = "'"
_APOSTROPHE_VARIANTS = {c: _APOS for c in "’‘‛ʼʹʻ`´�"}


def normalize_transcript(text: str) -> str:
    """NFC, apostrophe variants to ', line breaks to spaces, [..] and (..) removed, punctuation
    except ' removed, lower case, single spaces."""
    if not text or not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(str.maketrans(_APOSTROPHE_VARIANTS))
    text = re.sub(r"\r\n|\r|\n", " ", text)
    text = re.sub(r"\[.*?\]", " ", text)
    text = re.sub(r"\(.*?\)", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "P" or ch == _APOS)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()
