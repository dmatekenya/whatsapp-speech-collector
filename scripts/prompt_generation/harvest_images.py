"""Harvest picture-prompt candidates from image sources.

Example:
    uv run python -m scripts.prompt_generation.harvest_images --sources commons \
        --domains food,transport --limit-per-query 20 --run-id 2026-11-15-commons-test

Writes data/prompts/images/harvest/<run-id>/candidates.csv, thumbs/ and run-log.json.
Nothing is written to the review folder or the live prompts table.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from src.prompt_generation.config import load_config
from src.prompt_generation.harvest import format_summary, run_harvest
from src.prompt_generation.http import HttpClient
from src.prompt_generation.sources import build_source
try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; `uv run --env-file .env` also works
    load_dotenv = None

DEFAULT_CONFIG = Path("config/prompt-generation.yaml")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--sources", default="commons", help="comma-separated, e.g. commons,openverse,flickr")
    p.add_argument("--domains", default="all", help="comma-separated domain keys, or 'all'")
    p.add_argument("--limit-per-query", type=int, default=50)
    p.add_argument("--run-id", default=datetime.now().strftime("%Y-%m-%d-%H%M"))
    p.add_argument("--ocr", action="store_true", help="flag visible text (needs easyocr)")
    p.add_argument("--no-cache", action="store_true", help="ignore cached API responses")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv()
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)

    domain_keys = (
        [d.key for d in cfg.domains] if args.domains == "all"
        else [k.strip() for k in args.domains.split(",") if k.strip()]
    )
    for key in domain_keys:
        cfg.domain(key)  # fail early on a typo

    http = HttpClient(cfg.http, cfg.cache_dir, use_cache=not args.no_cache)
    sources = [build_source(s.strip(), http, cfg) for s in args.sources.split(",") if s.strip()]

    result = run_harvest(
        cfg, sources, domain_keys, args.run_id, args.limit_per_query,
        download=http.download, ocr=args.ocr,
    )
    print(format_summary(result, cfg))
    print(f"\nWrote {result.run_dir / 'candidates.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
