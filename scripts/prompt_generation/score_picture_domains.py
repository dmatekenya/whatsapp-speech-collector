"""Suggest a primary domain for each kept candidate with vision models via OpenRouter.

Examples:
    # see which vision models OpenRouter offers, with prices
    python3 -m scripts.prompt_generation.score_picture_domains --list-models

    # evaluation: score only the images you labelled, with each configured model
    python3 -m scripts.prompt_generation.score_picture_domains --run-id 2026-11-15-commons-test --labelled-only

    # same images without title/tags, to see whether metadata helps
    python3 -m scripts.prompt_generation.score_picture_domains --run-id 2026-11-15-commons-test --labelled-only --no-metadata

    # add the larger comparison model(s) from the config
    python3 -m scripts.prompt_generation.score_picture_domains --run-id 2026-11-15-commons-test --labelled-only --larger

Writes data/prompts/images/harvest/<run-id>/domain-scores.csv (appends; re-runs skip pairs
already scored).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from src.prompt_generation.config import load_config
from src.prompt_generation.picture_scoring import (
    OpenRouterClient, load_scoring_config, run_scoring,
)

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; `uv run --env-file .env` also works
    load_dotenv = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=Path("config/picture-domain-scoring.yaml"))
    p.add_argument("--harvest-config", type=Path, default=Path("config/prompt-generation.yaml"))
    p.add_argument("--run-id", help="harvest run folder name")
    p.add_argument("--models", help="comma-separated model IDs (default: models in config)")
    p.add_argument("--larger", action="store_true", help="use larger_models from the config")
    p.add_argument("--labelled-only", action="store_true",
                   help="score only candidates with a label in calibration-labels.csv")
    p.add_argument("--no-metadata", action="store_true", help="send the image without title/tags")
    p.add_argument("--limit", type=int, help="score at most this many candidates")
    p.add_argument("--max-spend", type=float, help="override max_spend_usd for this run")
    p.add_argument("--list-models", action="store_true", help="list OpenRouter vision models")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv()
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = load_scoring_config(args.config)
    client = OpenRouterClient(cfg)

    if args.list_models:
        for m in sorted(client.list_vision_models(), key=lambda m: m["id"]):
            price = m.get("pricing", {})
            per_m_in = float(price.get("prompt") or 0) * 1e6
            per_m_out = float(price.get("completion") or 0) * 1e6
            print(f"{m['id']:<55} in ${per_m_in:>7.2f}/M  out ${per_m_out:>7.2f}/M")
        return 0

    if not args.run_id:
        print("--run-id is required", file=sys.stderr)
        return 2
    harvest_cfg = load_config(args.harvest_config)
    run_dir = harvest_cfg.output_root / args.run_id

    models = (
        [m.strip() for m in args.models.split(",") if m.strip()] if args.models
        else cfg.larger_models if args.larger else cfg.models
    )

    candidate_ids = None
    if args.labelled_only:
        labels_path = run_dir / "calibration-labels.csv"
        if not labels_path.exists():
            print(f"No labels at {labels_path}; create them with "
                  "`python3 -m scripts.prompt_generation.picture_domain_eval sheet --run-id ...`", file=sys.stderr)
            return 2
        with open(labels_path, newline="", encoding="utf-8") as fh:
            candidate_ids = {r["candidate_id"] for r in csv.DictReader(fh) if r.get("label_domain")}

    summary = run_scoring(
        client, cfg, run_dir, models, candidate_ids=candidate_ids,
        include_metadata=False if args.no_metadata else None,
        limit=args.limit, max_spend_usd=args.max_spend,
    )
    print(json.dumps(summary, indent=2))
    if summary["skipped_budget"]:
        print("Some requests were skipped because the spend or call cap was reached.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
