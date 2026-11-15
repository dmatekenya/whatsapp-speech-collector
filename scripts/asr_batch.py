"""Transcribe stored recordings on the deployed Modal app and store the transcripts.

Run it through the wrapper, which picks uv (laptop) or the api container (droplet):

    ./scripts/asr_batch.sh --dry-run
    ./scripts/asr_batch.sh --include-test --participant P0006
    ./scripts/asr_batch.sh --since 2026-11-01 --until 2026-11-03 --limit 50
    ./scripts/asr_batch.sh --recordings 120,121 --force --model whisper_v1
    ./scripts/asr_batch.sh --scheduled          (hourly from cron: runs only at a slot)

Selection: stored recordings of registered participants with a WAV on disk;
not rejected at arrival (empty, too_short, too_long, no_speech, not_recorded);
not duplicates; not test participants (unless --include-test or
selection.include_test); not already transcribed by this model (unless
--force). Clips over 30 s go through the Modal app's long-form decoding.
Settings: config/asr_batch.yaml. Prints and logs recording IDs and counts only.
"""

import argparse
import logging
import signal
import sys
from datetime import UTC, date, datetime

from src.asr.batch import (
    BatchLocked,
    BatchReport,
    Filters,
    Selection,
    batch_lock,
    chunk_clips,
    mark_interrupted,
    ran_in_slot,
    run_batch,
    select_clips,
)
from src.asr.client import ModalTranscriber, Transcriber
from src.asr.config import (
    AsrBatchConfig,
    AsrConfigError,
    check_model_key,
    get_asr_config,
)
from src.logging_config import setup_logging
from src.storage.audio import get_audio_storage
from src.storage.db import get_sync_engine, get_sync_sessionmaker

logger = logging.getLogger("asr_batch")


# =======
# OPTIONS
# =======
def _ids(text: str) -> list[int]:
    try:
        return [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected recording IDs like 120,121, not {text!r}") from None


def _model(text: str) -> str:
    try:
        return check_model_key(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _positive(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", type=_model, help="model key (default: modal.model in config/asr_batch.yaml)")
    parser.add_argument("--since", type=date.fromisoformat, metavar="DATE", help="stored on or after (local date)")
    parser.add_argument("--until", type=date.fromisoformat, metavar="DATE", help="stored on or before (local date)")
    parser.add_argument("--participant", action="append", default=[], metavar="P0012", help="repeatable")
    parser.add_argument("--recordings", type=_ids, default=[], metavar="120,121")
    parser.add_argument("--include-test", action="store_true", help="include test participants (is_test)")
    parser.add_argument("--force", action="store_true", help="transcribe again even if this model already has")
    parser.add_argument("--limit", type=_positive, metavar="N", help="at most N clips")
    parser.add_argument("--dry-run", action="store_true", help="select and report; no Modal call, nothing saved")
    parser.add_argument("--scheduled", action="store_true", help="for cron: run only at a schedule slot, once")
    args = parser.parse_args(argv)
    if args.since and args.until and args.since > args.until:
        parser.error("--since is after --until")
    return args


# ======
# OUTPUT
# ======
def print_selection(selection: Selection, config: AsrBatchConfig, model_key: str) -> None:
    chunks = chunk_clips(selection.clips, config.modal.chunk_clips, config.modal.chunk_max_bytes)
    print(
        f"Selected {len(selection.clips)} clip(s) for {model_key}: {selection.audio_minutes:.1f} audio minutes,"
        f" {selection.size_mb:.1f} MB in {len(chunks)} chunk(s)"
    )
    for reason, count in sorted(selection.excluded.items()):
        print(f"  not sent, {reason}: {count}")
    if selection.missing_ids:
        print(f"  WAV missing: recording ids {', '.join(map(str, selection.missing_ids))}")


def print_report(report: BatchReport) -> None:
    revision = (report.model_revision or "?")[:12]
    print(
        f"Batch {report.batch_id} {report.status}: {report.clips_transcribed} transcribed,"
        f" {report.clips_failed} failed of {report.clips_selected} selected"
        f" ({report.clips_missing} WAV missing)"
    )
    print(
        f"  {report.audio_seconds / 60:.1f} audio minutes in {report.wall_seconds:.0f} s wall time;"
        f" GPU {report.gpu or '?'}; model {report.model_key} ({report.model_repo or '?'}@{revision})"
    )
    if report.failed_ids:
        print(f"  no transcript for recording ids {', '.join(map(str, report.failed_ids))} (retried next run)")
    if report.error:
        print(f"  error: {report.error}")


# ====
# MAIN
# ====
def _stop_on_sigterm() -> None:
    """docker stop / kill: end like Ctrl+C, so the batch is marked interrupted."""

    def handler(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)


def main(argv: list[str] | None = None, transcriber: Transcriber | None = None, now: datetime | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    try:
        config = get_asr_config()
    except AsrConfigError as exc:
        print(f"config/asr_batch.yaml: {exc}", file=sys.stderr)
        return 2
    fixed = now  # tests pass a fixed time; otherwise the real clock

    def clock() -> datetime:
        return fixed or datetime.now(UTC)

    now = clock()

    slot = None
    if args.scheduled:
        if config.schedule.paused:
            return 0  # runs_per_day: 0
        slot = config.schedule.current_slot(now)
        if slot is None:
            return 0  # not a slot hour

    filters = Filters(
        model_key=args.model or config.modal.model,
        since=args.since,
        until=args.until,
        participants=tuple(args.participant),
        recording_ids=tuple(args.recordings),
        include_test=args.include_test or config.selection.include_test,
        force=args.force,
        limit=args.limit,
    )
    factory = get_sync_sessionmaker()
    storage = get_audio_storage()
    tz = config.schedule.tz

    if args.dry_run:
        with factory() as db:
            selection = select_clips(db, filters, storage, tz)
        print("DRY RUN: no Modal call, nothing saved.")
        print_selection(selection, config, filters.model_key)
        return 0

    _stop_on_sigterm()
    try:
        with batch_lock(get_sync_engine()):
            with factory() as db:
                stale = mark_interrupted(db, now)
                if slot is not None and ran_in_slot(db, slot):
                    db.commit()
                    return 0  # cron fired twice in this slot
                db.commit()
            if stale:
                logger.warning("Marked %s earlier batch(es) that never finished as interrupted", stale)
            with factory() as db:
                selection = select_clips(db, filters, storage, tz)
            print_selection(selection, config, filters.model_key)
            report = run_batch(
                factory,
                transcriber or ModalTranscriber(config.modal.app_name, config.modal.class_name),
                selection,
                filters,
                trigger="scheduled" if args.scheduled else "manual",
                results_root=config.results_path,
                max_clips=config.modal.chunk_clips,
                max_bytes=config.modal.chunk_max_bytes,
                now=clock,
            )
    except BatchLocked as exc:
        print(f"Not started: {exc}.", file=sys.stderr)
        return 0 if args.scheduled else 3
    print_report(report)
    return 0 if report.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
