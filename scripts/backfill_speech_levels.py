"""Measure active_seconds and mean_dbfs for stored recordings that don't have them yet.

The worker measures every new voice note; this fills in recordings stored
before the speech check existed. It never changes qc_status or qc_reason: it
only reports how many would have been no_speech or low_speech under the
current config clip_checks, to help calibrate them.

    uv run python -m scripts.backfill_speech_levels --dry-run
    uv run python -m scripts.backfill_speech_levels
    uv run python -m scripts.backfill_speech_levels --all     (measure every stored recording again)

Prints recording IDs only, never names or phone numbers.
"""

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.audio.levels import measure_wav
from src.logging_config import setup_logging
from src.storage.audio import AudioStorage, get_audio_storage
from src.storage.db import get_sync_sessionmaker
from src.storage.models import Recording
from src.tasks.config import ClipChecks, get_collection_config

logger = logging.getLogger("backfill_speech_levels")
BATCH_SIZE = 200


# ========
# BACKFILL
# ========
@dataclass
class Result:
    measured: int = 0
    missing_file: list[int] = field(default_factory=list)
    unreadable: list[int] = field(default_factory=list)
    # What the current clip_checks would say (qc_status is not changed).
    would_be: Counter = field(default_factory=Counter)
    no_speech_ids: list[int] = field(default_factory=list)


def backfill(
    session_factory: sessionmaker[Session],
    storage: AudioStorage,
    checks: ClipChecks,
    remeasure: bool = False,
    dry_run: bool = False,
) -> Result:
    """Measure in batches by recording id, committing each batch (unless dry_run)."""
    result = Result()
    last_id = 0
    while True:
        with session_factory() as db:
            query = (
                select(Recording)
                .where(Recording.status == "stored", Recording.wav_path.is_not(None), Recording.id > last_id)
                .order_by(Recording.id)
                .limit(BATCH_SIZE)
            )
            if not remeasure:
                query = query.where(Recording.active_seconds.is_(None))
            batch = db.scalars(query).all()
            if not batch:
                return result
            for recording in batch:
                last_id = recording.id
                if not storage.exists(recording.wav_path):
                    result.missing_file.append(recording.id)
                    continue
                try:
                    levels = measure_wav(storage.absolute_path(recording.wav_path), checks.frame_ms, checks.active_dbfs)
                except ValueError as exc:
                    logger.warning("Recording id=%s: %s", recording.id, exc)
                    result.unreadable.append(recording.id)
                    continue
                recording.active_seconds = levels.active_seconds
                recording.mean_dbfs = levels.mean_dbfs
                result.measured += 1
                verdict = checks.problem(levels.active_seconds, recording.duration_s or levels.duration_s)
                result.would_be[verdict or "ok"] += 1
                if verdict == "no_speech":
                    result.no_speech_ids.append(recording.id)
            if dry_run:
                db.rollback()
            else:
                db.commit()


# ===
# CLI
# ===
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="measure and report, save nothing")
    parser.add_argument("--all", action="store_true", help="measure recordings that already have levels again")
    args = parser.parse_args(argv)
    setup_logging()

    checks = get_collection_config().clip_checks
    result = backfill(get_sync_sessionmaker(), get_audio_storage(), checks, remeasure=args.all, dry_run=args.dry_run)

    prefix = "DRY RUN: nothing saved. " if args.dry_run else ""
    print(f"{prefix}Measured {result.measured} recording(s).")
    print(
        f"Under the current clip_checks (min_active_seconds {checks.min_active_seconds:g},"
        f" min_active_ratio {checks.min_active_ratio:g}): "
        + ", ".join(f"{name} {result.would_be[name]}" for name in ("ok", "low_speech", "no_speech"))
        + " (qc_status not changed)"
    )
    if result.no_speech_ids:
        print("  would be no_speech: " + ", ".join(f"#{rid}" for rid in result.no_speech_ids))
    if result.missing_file:
        print(f"WAV file missing, skipped: {', '.join(f'#{rid}' for rid in result.missing_file)}")
    if result.unreadable:
        print(f"WAV file unreadable, skipped: {', '.join(f'#{rid}' for rid in result.unreadable)}")
    return 1 if result.missing_file or result.unreadable else 0


if __name__ == "__main__":
    sys.exit(main())
