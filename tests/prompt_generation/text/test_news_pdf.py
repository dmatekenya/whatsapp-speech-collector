"""Newspaper page reading: text layer vs OCR, dates, skips, OCR cache, and tool failures."""

from __future__ import annotations

import datetime as dt

import pytest

from src.prompt_generation.text import news_pdf
from src.prompt_generation.text.news_pdf import PdfSettings, parse_printed_date, resolve_date

from .conftest import FIXTURES, write_pdf

TEXT_LAYER = (FIXTURES / "page-text-layer.txt").read_text(encoding="utf-8")
OCR = (FIXTURES / "page-ocr.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("December 3 - 9, 2016", dt.date(2016, 12, 3)),
        ("February 13 - 19 , 2016", dt.date(2016, 2, 13)),
        ("June 26-July 2, 2021", dt.date(2021, 6, 26)),
        ("December 28 - January 3, 2017", dt.date(2016, 12, 28)),
    ],
)
def test_printed_date_takes_first_day_of_range(line, expected):
    assert parse_printed_date(f"Malawi News\n\n{line}\nbody text") == expected


def test_printed_date_only_searches_first_lines():
    text = "\n".join(["filler"] * 6 + ["December 3 - 9, 2016"])
    assert parse_printed_date(text) is None


def test_filename_date_is_used_when_no_printed_date():
    assert resolve_date("no date here", "TK1E2017_01_03_Page07.pdf") == (dt.date(2017, 1, 3), "filename")


def test_printed_date_wins_over_filename():
    assert resolve_date("June 26-July 2, 2021", "TK1E2017_01_03_Page07.pdf") == (dt.date(2021, 6, 26), "printed")


def test_no_date_anywhere_gives_none():
    assert resolve_date("no date", "page.pdf") == (None, "none")


def test_text_layer_is_used_when_it_has_enough_words(fake_tools, tmp_path):
    pdfs = tmp_path / "pdfs"
    write_pdf(pdfs, "FM1E2016_02_13_Page05.pdf")
    fake_tools.add("FM1E2016_02_13_Page05.pdf", text_layer=TEXT_LAYER)
    settings = PdfSettings(ocr_cache_dir=tmp_path / "cache")

    result = news_pdf.read_pdf_pages(pdfs, settings, ocr_workers=2)

    assert [(p.method, p.page, p.doc_date) for p in result.pages] == [
        ("text-layer", 5, dt.date(2016, 2, 13))
    ]
    assert fake_tools.count("tesseract") == 0


def test_short_text_layer_falls_back_to_ocr(fake_tools, tmp_path):
    pdfs = tmp_path / "pdfs"
    write_pdf(pdfs, "TK 5.pdf")
    fake_tools.add("TK 5.pdf", text_layer="Only a few words.", ocr_text=OCR)
    settings = PdfSettings(ocr_cache_dir=tmp_path / "cache")

    result = news_pdf.read_pdf_pages(pdfs, settings, ocr_workers=2)

    assert len(result.pages) == 1
    assert result.pages[0].method == "ocr"
    assert result.pages[0].doc_date == dt.date(2016, 12, 3)
    assert fake_tools.count("tesseract") == 1


def test_multi_image_page_is_rendered_with_pdftoppm(fake_tools, tmp_path):
    pdfs = tmp_path / "pdfs"
    write_pdf(pdfs, "TK 6.pdf")
    fake_tools.add("TK 6.pdf", images=3, ocr_text=OCR)
    settings = PdfSettings(ocr_cache_dir=tmp_path / "cache")

    result = news_pdf.read_pdf_pages(pdfs, settings, ocr_workers=1)

    assert result.pages[0].method == "ocr"
    assert fake_tools.count("pdftoppm") == 1
    assert fake_tools.count("pdfimages") == 1  # only the -list call, no -j extraction


def test_ocr_cache_skips_tesseract_on_second_run(fake_tools, tmp_path):
    pdfs = tmp_path / "pdfs"
    write_pdf(pdfs, "TK 5.pdf")
    fake_tools.add("TK 5.pdf", text_layer="", ocr_text=OCR)
    settings = PdfSettings(ocr_cache_dir=tmp_path / "cache")

    first = news_pdf.read_pdf_pages(pdfs, settings, ocr_workers=1)
    second = news_pdf.read_pdf_pages(pdfs, settings, ocr_workers=1)

    assert fake_tools.count("tesseract") == 1
    assert first.pages[0].ocr_cache_hit is False
    assert second.pages[0].ocr_cache_hit is True
    assert second.pages[0].text == OCR


def test_keyword_skip_applies_to_text_layer_pages(fake_tools, tmp_path):
    pdfs = tmp_path / "pdfs"
    write_pdf(pdfs, "FM1E2016_02_13_Page09.pdf")
    fake_tools.add("FM1E2016_02_13_Page09.pdf", text_layer=TEXT_LAYER + "\nMSCE results\n")
    settings = PdfSettings(page_skip_keywords=("MSCE",), ocr_cache_dir=tmp_path / "cache")

    result = news_pdf.read_pdf_pages(pdfs, settings, ocr_workers=1)

    assert result.pages == []
    assert result.skipped == [
        {"source_file": "FM1E2016_02_13_Page09.pdf", "page": 9, "reason": "keyword:MSCE"}
    ]


def test_multi_page_pdf_is_skipped_without_running_pdftotext(fake_tools, tmp_path):
    pdfs = tmp_path / "pdfs"
    write_pdf(pdfs, "KUKULA_ndi_MWAMBO.pdf")
    fake_tools.add("KUKULA_ndi_MWAMBO.pdf", pages=41, text_layer=TEXT_LAYER)
    settings = PdfSettings(ocr_cache_dir=tmp_path / "cache")

    result = news_pdf.read_pdf_pages(pdfs, settings, ocr_workers=1)

    assert result.pages == []
    assert result.skipped == [{"source_file": "KUKULA_ndi_MWAMBO.pdf", "reason": "multi_page:41"}]
    assert fake_tools.count("pdftotext") == 0


def test_non_pdf_files_are_counted_and_ignored(fake_tools, tmp_path):
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    (pdfs / "notes.txt").write_text("not a page", encoding="utf-8")

    result = news_pdf.read_pdf_pages(pdfs, PdfSettings(ocr_cache_dir=tmp_path / "cache"), ocr_workers=1)

    assert result.non_pdf_files == 1
    assert result.pages == []
    assert fake_tools.calls == []


def test_tool_failure_is_recorded_not_raised(monkeypatch, tmp_path):
    def missing(args, *, extra_env=None):
        raise news_pdf.ToolError(f"{args[0]} not found; install poppler and tesseract")

    monkeypatch.setattr(news_pdf, "run_tool", missing)
    pdfs = tmp_path / "pdfs"
    write_pdf(pdfs, "FM1E2016_02_13_Page05.pdf")

    result = news_pdf.read_pdf_pages(pdfs, PdfSettings(ocr_cache_dir=tmp_path / "cache"), ocr_workers=1)

    assert result.pages == []
    assert "pdfinfo not found" in result.failures[0]["error"]
