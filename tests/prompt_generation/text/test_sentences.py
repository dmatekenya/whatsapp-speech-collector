"""Stage 2 sentences: splitting, dedupe, the lexicon check and the kept CSV, on small fixtures."""

from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path

import pytest

from src.importers.framework import FieldError
from src.prompt_generation.text.sentences import (
    KEPT_COLUMNS,
    clean_unit,
    dedupe_key,
    load_sentence_settings,
    run_sentences,
    sentence_id,
    split_sentences,
)

ABBREVIATIONS = load_sentence_settings().abbreviations

S1 = "Mwana wake akupita kusukulu m'mawa uno ndipo amayi ake akupita naye limodzi kukakumana ndi aphunzitsi."
S3 = "Mtengo wa mbatata wakwera kwambiri pamsika wa Lilongwe ndipo anthu ambiri akuvutika kugula chakudya cha mabanja awo."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Iye anati “Ndabwera lero.” Ndiye anapita.",
            ["Iye anati “Ndabwera lero.”", "Ndiye anapita."],
        ),
        ("J. K. Rowling anali mlembi.", ["J. K. Rowling anali mlembi."]),
        ("Dr. Banda ananena lero.", ["Dr. Banda ananena lero."]),
        (
            "Mfumu T/A Kanyama ndi yabwino. Anthu akondwera.",
            ["Mfumu T/A Kanyama ndi yabwino.", "Anthu akondwera."],
        ),
        ("Ndinapita. kumudzi lero.", ["Ndinapita. kumudzi lero."]),
    ],
)
def test_split_sentences(text: str, expected: list[str]) -> None:
    assert split_sentences(text, ABBREVIATIONS) == expected


def test_dedupe_key_unifies_apostrophes_quotes_and_punctuation() -> None:
    assert dedupe_key("Mwana m’mawa “uno”.") == dedupe_key("mwana m'mawa uno")
    assert len(sentence_id(dedupe_key(S1))) == 16


def _write_run(root: Path, docs: list[dict]) -> None:
    run_dir = root / "extracted" / "r1"
    run_dir.mkdir(parents=True)
    with (run_dir / "documents.jsonl").open("w", encoding="utf-8") as handle:
        for doc in docs:
            handle.write(json.dumps(doc, ensure_ascii=False) + "\n")


def _doc(doc_id: str, method: str, blocks: list[str]) -> dict:
    return {
        "doc_id": doc_id, "source_category": "nation-publications", "source_reference": "Nation Publications",
        "method": method, "source_file": f"{doc_id}.xml", "page": None, "doc_title": doc_id,
        "doc_date": None, "date_source": "none", "blocks": blocks,
        "word_count": sum(len(b.split()) for b in blocks),
    }


def test_run_dedupes_within_run_and_against_bank(tmp_path: Path) -> None:
    _write_run(tmp_path, [
        _doc("nation-1", "wxr", [S1, S3, S1.replace("'", "’")]),
        _doc("ocr-1", "ocr", [S1.replace("'", "’"), "Zaka khumi zapitazo lero. " + "Mawu " * 10 + "atha."]),
    ])
    bank = tmp_path / "bank.csv"
    bank.write_text(f"text,source,domain\n{S3},old,TBD\n", encoding="utf-8")
    settings = dataclasses.replace(
        load_sentence_settings(),
        extracted_text_root=tmp_path / "extracted",
        sentences_root=tmp_path / "out",
        existing_sentences=bank,
    )

    run_sentences(run_id="r1", settings=settings)

    with (tmp_path / "out" / "r1" / "sentences-all.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    kept = [row for row in rows if row["status"] == "kept"]
    assert [row["text"] for row in kept] == [S1]
    kept_id = kept[0]["sentence_id"]
    duplicates = [row for row in rows if row["drop_reason"].startswith("duplicate_of")]
    assert len(duplicates) == 2
    assert {row["drop_reason"] for row in duplicates} == {f"duplicate_of:{kept_id}"}
    by_text = {row["text"]: row for row in rows}
    assert by_text[S3]["drop_reason"] == "already_in_bank"
    ocr = next(row for row in rows if row["text"].startswith("Zaka"))
    assert ocr["drop_reason"] == "ocr_unknown_words"
    assert ocr["unknown_words"].startswith("zaka|khumi|zapitazo|lero|mawu")


def test_kept_csv_matches_importer_columns_and_passes_its_check(tmp_path: Path) -> None:
    _write_run(tmp_path, [_doc("nation-1", "wxr", [S1, S3])])
    settings = dataclasses.replace(
        load_sentence_settings(),
        extracted_text_root=tmp_path / "extracted",
        sentences_root=tmp_path / "out",
        existing_sentences=tmp_path / "missing.csv",
    )

    run_sentences(run_id="r1", settings=settings)

    with (tmp_path / "out" / "r1" / "sentences-kept.csv").open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == KEPT_COLUMNS
        kept = list(reader)
    assert [row["source"] for row in kept] == ["nation-publications", "nation-publications"]
    assert all(row["domain"] == "" for row in kept)
    for row in kept:
        settings.importer.clean(row["text"])
    with pytest.raises(FieldError):
        settings.importer.clean("Mwana wake ali ndi zaka 5 lero.")


def _sentence(words: int, first: str = "Mwana") -> str:
    return " ".join([first, *(["wake"] * (words - 2)), "lero."])


@pytest.mark.parametrize(
    ("lengths", "counts", "types", "reasons"),
    [
        ([15, 18], [33], ["paragraph"], [""]),
        ([15, 18, 22, 15], [33, 37], ["group", "group"], ["", ""]),
        ([12], [12], ["paragraph"], ["too_short"]),
        ([48, 15], [48, 15], ["sentence", "sentence"], ["", ""]),
        ([20, 18, 10], [48], ["paragraph"], [""]),
        ([20, 18, 35, 10], [38, 45], ["group", "group"], ["", ""]),
        ([48, 10], [48, 10], ["sentence", "sentence"], ["", "too_short"]),
        ([56, 15], [56, 15], ["sentence", "sentence"], ["too_long", ""]),
        ([5, 10, 40, 15], [15, 40, 15], ["group", "sentence", "sentence"], ["", "", ""]),
    ],
)
def test_paragraph_and_group_units(tmp_path: Path, lengths, counts, types, reasons) -> None:
    sentences = [_sentence(n, first) for n, first in zip(lengths, ["Mwana", "Amayi", "Abambo", "Anthu"])]
    _write_run(tmp_path, [_doc("nation-1", "wxr", [" ".join(sentences)])])
    settings = dataclasses.replace(
        load_sentence_settings(), extracted_text_root=tmp_path / "extracted",
        sentences_root=tmp_path / "out", existing_sentences=tmp_path / "missing.csv",
    )
    run_sentences(run_id="r1", settings=settings)
    with (settings.sentences_root / "r1" / "sentences-all.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [int(row["word_count"]) for row in rows] == counts
    assert [row["unit_type"] for row in rows] == types
    assert [row["drop_reason"] for row in rows] == reasons
    assert sum(int(row["n_sentences"]) for row in rows) == len(lengths)
    assert " ".join(row["text"] for row in rows) == " ".join(sentences)
    assert all(row["doc_id"] == "nation-1" and row["block_index"] == "0" for row in rows)
    assert all("out_of_target" not in row for row in rows)
    for row in rows:
        if row["status"] == "kept":
            settings.importer.clean(row["text"])
            assert settings.importer.length_flag(row["text"]) is None


def test_invalid_sentence_breaks_groups_and_incomplete_sentence_is_dropped(tmp_path: Path) -> None:
    sentences = [_sentence(20), _sentence(15, "Amayi").replace("wake", "5", 1), _sentence(20, "Abambo")]
    _write_run(tmp_path, [_doc("nation-1", "wxr", [
        " ".join([*sentences, _sentence(20, "Anthu")]),
        _sentence(48) + " " + _sentence(15, "Amayi").rstrip("."),
    ])])
    settings = dataclasses.replace(
        load_sentence_settings(), extracted_text_root=tmp_path / "extracted",
        sentences_root=tmp_path / "out", existing_sentences=tmp_path / "missing.csv",
    )
    run_sentences(run_id="r1", settings=settings)
    with (settings.sentences_root / "r1" / "sentences-all.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["drop_reason"] for row in rows] == ["", "has_digit", "", "", "bad_end"]
    assert [row["word_count"] for row in rows] == ["20", "15", "40", "48", "15"]
    assert [row["n_sentences"] for row in rows] == ["1", "1", "2", "1", "1"]
    assert [row["block_index"] for row in rows] == ["0", "0", "0", "1", "1"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mwana m’‘nyumba lero.", "Mwana m'nyumba lero."),
        ('Mwana m”"nyumba lero.', "Mwana m'nyumba lero."),
        ("Mwana wake lero.....", "Mwana wake lero."),
        ("“Mwana wake lero...", "Mwana wake lero."),
        ("Mwana wake lero.”", "Mwana wake lero."),
        ('"Mwana wake lero.', "Mwana wake lero."),
        ('Mwana wake lero."', "Mwana wake lero."),
        ("“Mwana wake lero.”", "“Mwana wake lero.”"),
        ("Mwana anati “moni lero.", "Mwana anati “moni lero."),
    ],
)
def test_cleaning_quotes_apostrophes_and_trailing_dots(text, expected) -> None:
    assert clean_unit(text, "'") == expected


@pytest.mark.parametrize(
    ("ending", "reason"),
    [
        ("weniweni. koma lero.", "mid_sentence_break"),
        ("m’‘nyumba lero...", ""),
        ("lero.”", ""),
        ("anati “moni lero.", "unbalanced"),
    ],
)
def test_cleaning_and_filtering_in_pipeline(tmp_path: Path, ending, reason) -> None:
    _write_run(tmp_path, [_doc("nation-1", "wxr", ["Mwana " + "wake " * 14 + ending])])
    settings = dataclasses.replace(
        load_sentence_settings(), extracted_text_root=tmp_path / "extracted",
        sentences_root=tmp_path / "out", existing_sentences=tmp_path / "missing.csv",
    )
    run_sentences(run_id="r1", settings=settings)
    with (settings.sentences_root / "r1" / "sentences-all.csv").open(encoding="utf-8") as handle:
        (row,) = csv.DictReader(handle)
    assert row["drop_reason"] == reason
    if not reason:
        assert "’" not in row["text"] and "..." not in row["text"]
