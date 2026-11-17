"""End-to-end Stage 1 run with fake poppler/tesseract and the WXR fixture; checks every output file."""

from __future__ import annotations

import csv
import dataclasses
import json
import textwrap

import pytest

from scripts.prompt_generation.extract_sentences import format_summary
from src.prompt_generation.text.extract import load_config, run_extraction

from .conftest import FIXTURES, write_pdf

TEXT_LAYER = (FIXTURES / "page-text-layer.txt").read_text(encoding="utf-8")
OCR = "\n\n".join(
    textwrap.fill(block, width=70)
    for block in (FIXTURES / "page-ocr.txt").read_text(encoding="utf-8").split("\n\n")
)


@pytest.fixture
def config(tmp_path):
    cfg = load_config()
    return dataclasses.replace(
        cfg,
        nation_xml=FIXTURES / "wxr-sample.xml",
        malawi_news_dir=tmp_path / "pdfs",
        extracted_text_root=tmp_path / "out",
        pdf=dataclasses.replace(cfg.pdf, ocr_cache_dir=tmp_path / "cache"),
    )


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_pdf_and_wxr_run_writes_all_outputs(fake_tools, config, tmp_path):
    pdfs = config.malawi_news_dir
    write_pdf(pdfs, "FM1E2016_02_13_Page05.pdf")
    write_pdf(pdfs, "TK 5.pdf")
    write_pdf(pdfs, "KUKULA_ndi_MWAMBO.pdf")
    write_pdf(pdfs, "FM1E2016_02_13_Page09.pdf")
    (pdfs / "readme.txt").write_text("notes", encoding="utf-8")
    fake_tools.add("FM1E2016_02_13_Page05.pdf", text_layer=TEXT_LAYER)
    fake_tools.add("TK 5.pdf", text_layer="", ocr_text=OCR)
    fake_tools.add("KUKULA_ndi_MWAMBO.pdf", pages=41, text_layer=TEXT_LAYER)
    fake_tools.add("FM1E2016_02_13_Page09.pdf", text_layer=TEXT_LAYER + "\nMSCE\n")

    summary = run_extraction(run_id="test-run", config=config)

    out = config.extracted_text_root / "test-run"
    docs = _read_jsonl(out / "documents.jsonl")
    by_method = {}
    for doc in docs:
        by_method.setdefault(doc["method"], []).append(doc)

    assert sorted(by_method) == ["ocr", "text-layer", "wxr"]
    assert len(by_method["wxr"]) == 2
    wxr_doc = next(d for d in docs if d["doc_id"] == "wxr-101")
    assert wxr_doc["source_category"] == "nation-publications"
    assert wxr_doc["source_reference"] == "Nation Publications"
    assert wxr_doc["doc_date"] == "2023-01-05"
    assert wxr_doc["date_source"] == "post"
    assert wxr_doc["page"] is None
    assert wxr_doc["word_count"] == sum(len(b.split()) for b in wxr_doc["blocks"])

    text_doc = by_method["text-layer"][0]
    assert text_doc["source_category"] == "malawi-news"
    assert text_doc["source_reference"] == "Malawi News"
    assert text_doc["page"] == 5
    assert (text_doc["doc_date"], text_doc["date_source"]) == ("2016-02-13", "printed")

    ocr_doc = by_method["ocr"][0]
    assert (ocr_doc["doc_date"], ocr_doc["date_source"]) == ("2016-12-03", "printed")

    for doc in docs:
        text_file = out / "texts" / f"{doc['doc_id']}.txt"
        assert text_file.read_text(encoding="utf-8").strip() == "\n\n".join(doc["blocks"])

    with (out / "blocks-debug.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {"header", "byline", "photo_credit", "caption", "headline"} <= {r["reason"] for r in rows}
    assert all(r["doc_id"] for r in rows)

    log = json.loads((out / "run-log.json").read_text(encoding="utf-8"))
    assert log["documents_per_method"] == {"wxr": 2, "text-layer": 1, "ocr": 1}
    assert log["documents_per_source_category"] == {"nation-publications": 2, "malawi-news": 2}
    assert log["ocr_pages_run"] == 1
    assert log["non_pdf_files"] == 1
    assert {"source_file": "KUKULA_ndi_MWAMBO.pdf", "reason": "multi_page:41"} in log["skipped"]
    assert {"source_file": "FM1E2016_02_13_Page09.pdf", "page": 9, "reason": "keyword:MSCE"} in log["skipped"]
    assert log["no_date_documents"] == []
    assert summary["output_dir"] == str(out)
    formatted = format_summary({**log, "output_dir": summary["output_dir"]})
    assert "Run test-run: 4 documents" in formatted
    assert "source nation-publications: 2" in formatted
    assert "source malawi-news: 2" in formatted
    assert "method ocr: 1" in formatted
