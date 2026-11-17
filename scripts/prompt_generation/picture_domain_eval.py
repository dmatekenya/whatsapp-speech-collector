"""Calibration tools for domain scoring.

    # 1. make a labelling page for a harvest run, open it in a browser, label, export CSV
    python3 -m scripts.prompt_generation.picture_domain_eval sheet --run-id 2026-11-15-commons-test

    # 2. after scoring (scripts/prompt_generation/score_picture_domains.py --labelled-only), compare models with labels
    python3 -m scripts.prompt_generation.picture_domain_eval report --run-id 2026-11-15-commons-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.prompt_generation.config import load_config
from src.prompt_generation.picture_evaluation import build_label_sheet, evaluate, format_report
from src.prompt_generation.picture_scoring import load_scoring_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["sheet", "report"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--config", type=Path, default=Path("config/picture-domain-scoring.yaml"))
    p.add_argument("--harvest-config", type=Path, default=Path("config/prompt-generation.yaml"))
    p.add_argument("--sample-per-domain", type=int, help="label a random sample per domain")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    run_dir = load_config(args.harvest_config).output_root / args.run_id
    if not (run_dir / "candidates.csv").exists():
        print(f"No candidates.csv in {run_dir}", file=sys.stderr)
        return 2

    if args.command == "sheet":
        out = build_label_sheet(run_dir, load_scoring_config(args.config).domains,
                                sample_per_domain=args.sample_per_domain, seed=args.seed)
        print(f"Wrote {out}\nOpen it in a browser, label, then Export and save the file as "
              f"{run_dir / 'calibration-labels.csv'}")
        return 0

    baseline, results = evaluate(run_dir)
    report = format_report(baseline, results)
    (run_dir / "domain-eval-report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
