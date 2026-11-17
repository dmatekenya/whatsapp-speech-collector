"""Chichewa text pipeline: extract documents, prepare reading units, suggest domains.

Examples:
    uv run python -m scripts.prompt_generation.extract_sentences extract --run-id 2026-01 -v
    uv run python -m scripts.prompt_generation.extract_sentences sentences --run-id 2026-01
    uv run python -m scripts.prompt_generation.extract_sentences domains --run-id 2026-01

Raw inputs, output roots and settings come from config/sentence-extraction.yaml and
config/domains.yaml. Raw inputs are only read. OCR needs poppler and tesseract; domains
needs OPENROUTER_API_KEY (from .env).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from src.prompt_generation.text.domains import load_domain_settings, run_domains
from src.prompt_generation.text.extract import load_config, run_extraction
from src.prompt_generation.text.sentences import load_sentence_settings, run_sentences

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; `uv run --env-file .env` also works
    load_dotenv = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    ext = sub.add_parser("extract", help="stage 1: documents and kept body blocks")
    ext.add_argument("--run-id", required=True)
    ext.add_argument("--ocr-workers", type=int, help="parallel OCR jobs (default: config ocr_workers)")
    ext.add_argument("-v", "--verbose", action="store_true")
    for name, help_text in (
        ("sentences", "stage 2: prepare, filter, dedupe and write reading-unit CSVs"),
        ("domains", "stage 3: suggest one domain per kept sentence (OpenRouter)"),
    ):
        stage = sub.add_parser(name, help=help_text)
        stage.add_argument("--run-id", required=True)
        stage.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def format_summary(run: dict) -> str:
    lines = [f"Run {run['run_id']}: {run['documents_total']} documents"]
    for source, count in sorted(run["documents_per_source_category"].items()):
        lines.append(f"  source {source}: {count}")
    for method, count in sorted(run["documents_per_method"].items()):
        lines.append(f"  method {method}: {count}")
    if run["dropped_blocks_by_reason"]:
        lines.append("Dropped blocks by reason:")
        for reason, count in sorted(run["dropped_blocks_by_reason"].items()):
            lines.append(f"  {reason}: {count}")
    lines.append(
        f"Skipped: {len(run.get('skipped', []))}  failures: {len(run.get('failures', []))}  "
        f"OCR run: {run.get('ocr_pages_run', 0)}  OCR cache hits: {run.get('ocr_cache_hits', 0)}  "
        f"no date: {len(run['no_date_documents'])}"
    )
    lines.append(f"Output: {run['output_dir']}")
    return "\n".join(lines)


def format_sentences_summary(run: dict) -> str:
    lines = [
        f"Run {run['run_id']}: {run['documents']} documents, {run['sentences_found']} units, "
        f"{run['sentences_kept']} kept"
    ]
    for label, groups in (("source", run["by_source_category"]), ("method", run["by_method"])):
        for key, counts in sorted(groups.items()):
            lines.append(f"  {label} {key}: found {counts['found']}, kept {counts['kept']}")
    if run["drop_reasons"]:
        lines.append("Dropped by reason:")
        for reason, count in sorted(run["drop_reasons"].items(), key=lambda item: -item[1]):
            lines.append(f"  {reason}: {count}")
    lines.append(f"Output: {run['output_dir']}")
    return "\n".join(lines)


def format_domains_summary(summary: dict) -> str:
    lines = [
        f"Domains with {summary['model']}: scored {summary['scored']}, unscored {summary['unscored']}, "
        f"already scored {summary['already_scored']}, remaining {summary['remaining']}",
        f"Calls {summary['calls']}, spent ${summary['spent_usd']:.4f} of ${summary['max_spend_usd']:.2f} cap"
        + (" (stopped at the cap; run again to continue)" if summary["stopped_by_limit"] else ""),
    ]
    lines += [f"Error: {error}" for error in summary["errors"][:5]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if load_dotenv:
        load_dotenv()

    if args.command == "extract":
        config = load_config()
        if args.ocr_workers is not None:
            config = replace(config, ocr_workers=args.ocr_workers)
        print(format_summary(run_extraction(run_id=args.run_id, config=config)))
    elif args.command == "sentences":
        run = run_sentences(run_id=args.run_id, settings=load_sentence_settings())
        print(format_sentences_summary(run))
    else:
        summary = run_domains(run_id=args.run_id, settings=load_domain_settings())
        print(format_domains_summary(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
