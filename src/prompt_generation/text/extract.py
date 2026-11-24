"""Stage 1: turn WXR posts and newspaper page PDFs into per-document blocks for one run."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.prompt_generation.text.blocks import BlockRules, clean_blocks
from src.prompt_generation.text.news_pdf import PdfSettings, read_pdf_pages
from src.prompt_generation.text.wxr import parse_wxr

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "sentence-extraction.yaml"
NATION_CATEGORY = "nation-publications"
NATION_REFERENCE = "Nation Publications"
MALAWI_NEWS_CATEGORY = "malawi-news"
MALAWI_NEWS_REFERENCE = "Malawi News"

# ======
# CONFIG
# ======


@dataclass(frozen=True)
class ExtractConfig:
    nation_xml: Path
    malawi_news_dir: Path
    extracted_text_root: Path
    ocr_workers: int
    pdf: PdfSettings
    blocks: BlockRules


def load_config(path: Path = DEFAULT_CONFIG) -> ExtractConfig:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_root = PROJECT_ROOT / raw["raw_root"]
    pdf = PdfSettings(
        min_text_layer_words=int(raw["min_text_layer_words"]),
        ocr_lang=str(raw["ocr_lang"]),
        ocr_psm=int(raw["ocr_psm"]),
        ocr_fallback_dpi=int(raw["ocr_fallback_dpi"]),
        page_skip_keywords=tuple(raw["page_skip_keywords"]),
        ocr_cache_dir=PROJECT_ROOT / raw["ocr_cache_dir"],
    )
    blocks = BlockRules(
        header_patterns=tuple(raw["header_patterns"]),
        byline_prefixes=tuple(raw["byline_prefixes"]),
        byline_ndi_max_words=int(raw["byline_ndi_max_words"]),
        photo_credit=str(raw["photo_credit"]),
        min_headline_words=int(raw["min_headline_words"]),
        drop_cap_repair=bool(raw["drop_cap_repair"]),
        ocr_max_line_chars=int(raw.get("ocr_max_line_chars", 70)),
    )
    return ExtractConfig(
        nation_xml=raw_root / raw["nation_xml"],
        malawi_news_dir=raw_root / raw["malawi_news_dir"],
        extracted_text_root=PROJECT_ROOT / raw["extracted_text_root"],
        ocr_workers=int(raw.get("ocr_workers", 4)),
        pdf=pdf,
        blocks=blocks,
    )


# =========
# DOCUMENTS
# =========


def _word_count(blocks: list[str]) -> int:
    return sum(len(block.split()) for block in blocks)


def _pdf_doc_id(relpath: str) -> str:
    stem = Path(relpath).with_suffix("").as_posix()
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:80]
    return f"{slug}-{hashlib.sha1(relpath.encode('utf-8')).hexdigest()[:8]}"


def _document(**fields: Any) -> dict[str, Any]:
    blocks = fields["blocks"]
    return {**fields, "word_count": _word_count(blocks)}


def wxr_documents(wxr_path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    posts, skipped = parse_wxr(wxr_path)
    documents = []
    for post in posts:
        if not post.paragraphs:
            continue
        documents.append(
            _document(
                doc_id=f"wxr-{post.post_id}",
                source_category=NATION_CATEGORY,
                source_reference=NATION_REFERENCE,
                method="wxr",
                source_file=wxr_path.name,
                page=None,
                doc_title=post.title,
                doc_date=post.post_date.isoformat() if post.post_date else None,
                date_source="post" if post.post_date else "none",
                categories=post.categories,
                blocks=post.paragraphs,
            )
        )
    return documents, skipped


def pdf_documents(pdf_dir: Path, config: ExtractConfig) -> tuple[list[dict[str, Any]], list[dict], dict[str, Any]]:
    read = read_pdf_pages(pdf_dir, config.pdf, ocr_workers=config.ocr_workers)
    documents: list[dict[str, Any]] = []
    debug_rows: list[dict[str, Any]] = []
    for page in read.pages:
        doc_id = _pdf_doc_id(page.source_file)
        cleaned = clean_blocks(page.text, config.blocks, method=page.method)
        for dropped in cleaned.dropped:
            debug_rows.append(
                {
                    "doc_id": doc_id,
                    "source_file": page.source_file,
                    "page": page.page,
                    "block_index": dropped.index,
                    "reason": dropped.reason,
                    "text": dropped.text,
                }
            )
        if not cleaned.kept:
            read.skipped.append({"source_file": page.source_file, "page": page.page, "reason": "no_kept_blocks"})
            continue
        documents.append(
            _document(
                doc_id=doc_id,
                source_category=MALAWI_NEWS_CATEGORY,
                source_reference=MALAWI_NEWS_REFERENCE,
                method=page.method,
                source_file=page.source_file,
                page=page.page,
                doc_title=Path(page.source_file).stem,
                doc_date=page.doc_date.isoformat() if page.doc_date else None,
                date_source=page.date_source,
                blocks=cleaned.kept,
            )
        )
    extras = {
        "non_pdf_files": read.non_pdf_files,
        "skipped": read.skipped,
        "failures": read.failures,
        "ocr_cache_hits": sum(1 for p in read.pages if p.ocr_cache_hit),
        "ocr_pages_run": sum(1 for p in read.pages if p.method == "ocr" and not p.ocr_cache_hit),
    }
    return documents, debug_rows, extras


# ======
# OUTPUT
# ======


def _write_outputs(out_dir: Path, documents: list[dict[str, Any]], debug_rows: list[dict[str, Any]], run_log: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    texts_dir = out_dir / "texts"
    texts_dir.mkdir(exist_ok=True)
    for stale in texts_dir.glob("*.txt"):
        stale.unlink()

    with (out_dir / "documents.jsonl").open("w", encoding="utf-8") as handle:
        for doc in documents:
            handle.write(json.dumps(doc, ensure_ascii=False) + "\n")
            (texts_dir / f"{doc['doc_id']}.txt").write_text("\n\n".join(doc["blocks"]) + "\n", encoding="utf-8")

    with (out_dir / "blocks-debug.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["doc_id", "source_file", "page", "block_index", "reason", "text"])
        writer.writeheader()
        writer.writerows(debug_rows)

    (out_dir / "run-log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def run_extraction(*, run_id: str, config: ExtractConfig) -> dict[str, Any]:
    """Extract documents and blocks for one run from the fixed raw inputs, write outputs and return the run log."""
    documents: list[dict[str, Any]] = []
    debug_rows: list[dict[str, Any]] = []
    run_log: dict[str, Any] = {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat()}

    wxr_docs, wxr_skipped = wxr_documents(config.nation_xml)
    documents.extend(wxr_docs)
    run_log["nation_xml"] = {"file": config.nation_xml.name, "skipped_items": wxr_skipped}

    pdf_docs, pdf_debug, extras = pdf_documents(config.malawi_news_dir, config)
    documents.extend(pdf_docs)
    debug_rows.extend(pdf_debug)
    run_log["malawi_news_dir"] = str(config.malawi_news_dir)
    run_log.update({k: v for k, v in extras.items() if k not in {"skipped", "failures"}})
    run_log["skipped"] = extras["skipped"]
    run_log["failures"] = extras["failures"]

    run_log["documents_total"] = len(documents)
    run_log["documents_per_source_category"] = dict(Counter(d["source_category"] for d in documents))
    run_log["documents_per_method"] = dict(Counter(d["method"] for d in documents))
    run_log["dropped_blocks_by_reason"] = dict(Counter(r["reason"] for r in debug_rows))
    run_log["no_date_documents"] = [d["doc_id"] for d in documents if d["date_source"] == "none"]

    out_dir = config.extracted_text_root / run_id
    _write_outputs(out_dir, documents, debug_rows, run_log)
    log.info("wrote %d documents to %s", len(documents), out_dir)
    return {**run_log, "output_dir": str(out_dir)}
