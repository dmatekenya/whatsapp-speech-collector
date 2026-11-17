"""Block cleaning for newspaper pages: headers, bylines, captions, drop caps, joins and headlines."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.prompt_generation.text.blocks import BlockRules, clean_blocks, split_blocks
from src.prompt_generation.text.extract import load_config

from .conftest import FIXTURES

TEXT_LAYER = (FIXTURES / "page-text-layer.txt").read_text(encoding="utf-8")
RULES = load_config().blocks


def _reasons(result):
    return {d.reason: d.text for d in result.dropped}


def test_split_blocks_joins_lines_and_skips_blank_chunks():
    assert split_blocks("one\ntwo\n\n\n  \nthree") == ["one two", "three"]


def test_text_layer_headers_bylines_captions_and_photo_credit_are_dropped():
    result = clean_blocks(TEXT_LAYER, RULES, method="text-layer")
    dropped = [(d.reason, d.text) for d in result.dropped]

    assert ("header", "Malawi News / Generation / February 13—19, 2016/") in dropped
    assert ("header", "fiKAMBE Malawi News") in dropped
    assert ("header", "3") in dropped
    assert ("byline", "Wolemba Matilda Chimwaza") in dropped
    assert ("byline", "Ndi Mynard Nyirenda") in dropped
    assert ("photo_credit", "Wojambula: Feston Malekezo") in dropped
    assert ("caption", "KUCHITA ZOYENERA—Ana akukonza sukulu") in dropped
    assert all("Malawi News" not in block for block in result.kept)


def test_ndi_phrase_longer_than_four_words_is_kept():
    text = "Ndi nkhani yaikulu yomwe anthu ambiri akuidziwa bwino m'dziko lonse la Malawi lero."
    result = clean_blocks(text, RULES, method="text-layer")

    assert result.kept == [text]


def test_drop_cap_is_repaired_on_text_layer_pages():
    result = clean_blocks(TEXT_LAYER, RULES, method="text-layer")

    assert any(block.startswith("Mtengo wa ana ndi nkhani") for block in result.kept)
    assert "M" not in result.kept


def test_drop_cap_is_not_repaired_on_ocr_pages():
    text = "M\n\ntengo wa ana ndi nkhani yayikulu m'boma la Mulanje,\nkumene ophunzira ambiri akugwira ntchito."
    result = clean_blocks(text, RULES, method="ocr")

    assert not any(block.startswith("Mtengo") for block in result.kept)


def test_column_join_merges_mid_sentence_block_with_lowercase_continuation():
    result = clean_blocks(TEXT_LAYER, RULES, method="text-layer")

    joined = [b for b in result.kept if b.startswith("Ophunzira ochokera")]
    assert len(joined) == 1
    assert "anachita nawo ntchito yobzala mitengo" in joined[0]


def test_short_block_without_terminal_punctuation_is_a_headline():
    text = "Senzosami shines at Malosa Secondary School\n\nZikomo kwambiri."
    result = clean_blocks(text, RULES, method="text-layer")

    assert result.kept == ["Zikomo kwambiri."]
    assert ("headline", "Senzosami shines at Malosa Secondary School") in [
        (d.reason, d.text) for d in result.dropped
    ]


def test_dropped_blocks_keep_their_original_index_and_are_sorted():
    result = clean_blocks(TEXT_LAYER, RULES, method="text-layer")
    indexes = [d.index for d in result.dropped]

    assert indexes == sorted(indexes)
    assert _reasons(result)["photo_credit"] == "Wojambula: Feston Malekezo"


def test_custom_header_pattern_drops_matching_block():
    rules = BlockRules(header_patterns=(r"ZOCHITIKA",), min_headline_words=3)
    result = clean_blocks("ZOCHITIKA\n\nMwana wina anati ntchitoyi yamuphunzitsa.", rules, method="ocr")

    assert result.kept == ["Mwana wina anati ntchitoyi yamuphunzitsa."]
    assert result.dropped[0].reason == "header"


def test_merged_column_block_is_dropped_only_for_ocr():
    text = "Mwana " + "wake " * 15 + "lero."
    result = clean_blocks(text, RULES, method="ocr")
    assert result.kept == []
    assert [(drop.index, drop.reason, drop.text) for drop in result.dropped] == [(0, "merged_columns", text)]
    assert clean_blocks(text, RULES, method="text-layer").kept == [text]


def test_ocr_line_limit_checks_original_lines_and_is_inclusive():
    line = "Mwana wake akupita."
    rules = replace(RULES, ocr_max_line_chars=len(line))
    result = clean_blocks(f"{line}\n{line}", rules, method="ocr")
    assert result.kept == [f"{line} {line}"]
    result = clean_blocks(f"{line}x\n{line}", rules, method="ocr")
    assert result.dropped[0].reason == "merged_columns"


@pytest.mark.parametrize("method", ["ocr", "text-layer"])
def test_ocr_repairs_words_and_separator_noise_only_on_ocr(method):
    text = "lye ali___bwino = koma § anthu_ena=akumuthandiza lero."
    expected = "Iye ali bwino koma anthu ena akumuthandiza lero."
    assert clean_blocks(text, RULES, method=method).kept == [expected if method == "ocr" else text]
    assert clean_blocks("Alye ndi lyetu lero.", RULES, method=method).kept == ["Alye ndi lyetu lero."]
