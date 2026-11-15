"""Tier-2 scoring (ASR batch step 2): metrics and a level 1-4 for transcribed recordings.

Since 2026-11-11 only sentences get a level, from CER; other task types get every metric but
no level ("not applicable"; recordings.qc_tier2 empty). See config/qc_tier2.yaml.

On the droplet (inside the api container):

    docker compose exec api python -m scripts.score_tier2 --dry-run
    docker compose exec api python -m scripts.score_tier2
    docker compose exec api python -m scripts.score_tier2 --rescore              (after a cut-off change)
    docker compose exec api python -m scripts.score_tier2 --report-only --sample 10

Default: eligible recordings with no result yet for the current thresholds_version.
--rescore: every eligible recording again (new rows; old rows are kept). --since (Malawi
date), --participants, --recordings, --include-test and --limit narrow the selection.
--dry-run computes and prints the summary but writes nothing. --report-only prints the
report from stored results (the latest per recording for the current thresholds_version).
--sample N also writes data/qc/tier2-sample-<thresholds_version>.csv: up to N recordings per
task type and level, at random with a fixed seed, for listening. --report-file PATH also saves
the printed report to PATH. The report starts with the number of eligible clips still without a
transcript (waiting for the ASR batch).

The ASR batch scores what it transcribes by itself; this script is for backfills, re-scores
and reports. Cut-offs: config/qc_tier2.yaml. Writes only recordings.snr_db (if missing),
qc_results and recordings.qc_tier2; never qc_status. Prints IDs and numbers only.
Exit status: 0 done, 1 some recordings failed, 2 config error, 3 another run in progress.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from src.config import PROJECT_ROOT
from src.logging_config import setup_logging
from src.qc.levels import QcConfigError, load_qc_config
from src.qc.report import SAMPLE_SEED, render, write_sample
from src.qc.scoring import (
    Filters,
    ScoringLocked,
    count_untranscribed,
    run_scoring,
    scoring_lock,
    stored_rows,
)
from src.storage.db import get_sync_engine, get_sync_sessionmaker

SAMPLE_DIR = PROJECT_ROOT / "data" / "qc"


def _ids(text: str) -> list[int]:
    try:
        return [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected recording IDs like 120,121, not {text!r}") from None


def _participants(text: str) -> list[str]:
    return [part.strip().upper() for part in text.split(",") if part.strip()]


def _positive(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rescore", action="store_true", help="score every eligible recording again (new rows)")
    parser.add_argument("--since", type=date.fromisoformat, metavar="DATE", help="recordings stored on or after (Malawi date)")
    parser.add_argument("--participants", type=_participants, default=[], metavar="P0008,P0009")
    parser.add_argument("--recordings", type=_ids, default=[], metavar="120,121")
    parser.add_argument("--include-test", action="store_true", help="include test participants (is_test)")
    parser.add_argument("--limit", type=_positive, metavar="N", help="at most N recordings")
    parser.add_argument("--dry-run", action="store_true", help="compute and print the summary; write nothing")
    parser.add_argument("--report-only", action="store_true", help="print the report from stored results; score nothing")
    parser.add_argument("--sample", type=_positive, metavar="N", help="write a listening sample of up to N per task type and level")
    parser.add_argument("--report-file", type=Path, metavar="PATH", help="also save the printed report to this file")
    return parser.parse_args(argv)


def _shown(path: Path) -> str:
    """The path relative to the project (data/qc/...) when it is inside it."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    try:
        config = load_qc_config()
    except QcConfigError as exc:
        print(f"config/qc_tier2.yaml: {exc}", file=sys.stderr)
        return 2
    filters = Filters(
        recording_ids=tuple(args.recordings),
        participants=tuple(args.participants),
        since=args.since,
        include_test=args.include_test,
        rescore=args.rescore,
        limit=args.limit,
    )
    factory = get_sync_sessionmaker()
    versions = f"thresholds {config.thresholds_version}, metrics {config.metrics_version}, model {config.model}"
    status = 0
    report: list[str] = []

    def out(text: str = "") -> None:
        print(text)
        report.append(text)

    with factory() as db:
        untranscribed = count_untranscribed(db, config, filters)
    if not args.report_only:
        try:
            with scoring_lock(get_sync_engine()):
                result = run_scoring(factory, config, filters, dry_run=args.dry_run)
        except ScoringLocked as exc:
            print(f"Not started: {exc}.", file=sys.stderr)
            return 3
        label = "DRY RUN, nothing written" if args.dry_run else "this run"
        out(render(result.rows, f"Tier-2 scoring ({label}; {versions})", untranscribed))
        if result.failed_ids:
            out(f"\nNot scored (see the log; retried next run): recording ids {', '.join(map(str, result.failed_ids))}")
            status = 1
    else:
        with factory() as db:
            rows = stored_rows(db, config, filters)
        out(render(rows, f"Tier-2 report (stored results, latest per recording; {versions})", untranscribed))

    if args.sample:
        if args.dry_run:
            out("\nSample not written (dry run).")
        else:
            with factory() as db:
                rows = stored_rows(db, config, filters)
            path = SAMPLE_DIR / f"tier2-sample-{config.thresholds_version}.csv"
            written = write_sample(rows, args.sample, path)
            out(f"\nListening sample: {written} recording(s), up to {args.sample} per task type and level"
                f" (seed {SAMPLE_SEED}), in {_shown(path)}")  # fmt: skip
    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"\nReport saved to {_shown(args.report_file.resolve())}")
    return status


if __name__ == "__main__":
    sys.exit(main())
