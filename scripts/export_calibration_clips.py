"""Export QC calibration metadata or a blind bundle of WAVs for raters."""

import argparse
import csv
import os
import shutil
from pathlib import Path
from random import SystemRandom
from uuid import uuid4

from sqlalchemy import select

from src.storage.audio import get_audio_storage
from src.storage.db import get_sync_sessionmaker
from src.storage.models import CalibrationClip, Enrollment, Recording

MEASUREMENTS = (
    "duration_s", "active_seconds", "mean_dbfs", "length_flag", "audio_origin", "origin_policy",
    "below_minimum", "above_maximum", "speech_problem", "duplicate",
)
FIELDS = (
    "participant_id", "clip_id", "condition", "task", "task_position", "prompt_id",
    "attempt", "suggested_condition", "order_position", "original_path", "wav_path", *MEASUREMENTS,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory for the export")
    parser.add_argument("--blind", action="store_true", help="export anonymized WAV filenames and a rater CSV")
    args = parser.parse_args()
    with get_sync_sessionmaker()() as db:
        rows = db.execute(
            select(CalibrationClip, Enrollment.participant_id, Recording.original_path, Recording.wav_path)
            .join(Enrollment, Enrollment.id == CalibrationClip.enrollment_id)
            .join(Recording, Recording.id == CalibrationClip.recording_id)
            .where(Recording.status == "stored")
            .order_by(Enrollment.participant_id, CalibrationClip.order_position,
                      CalibrationClip.task_position, CalibrationClip.attempt)
        ).all()

    output = args.output
    output.mkdir(mode=0o700)
    try:
        if args.blind:
            SystemRandom().shuffle(rows)
            storage = get_audio_storage()
            key = []
            with (output / "clips.csv").open("x", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(("clip_id", "wav_path"))
                for clip, _, _, wav in rows:
                    rater_id = uuid4().hex
                    filename = f"{rater_id}.wav"
                    shutil.copyfile(storage.absolute_path(wav), output / filename)
                    writer.writerow((rater_id, filename))
                    key.append((rater_id, clip.id))
            # Keep the matching key beside the bundle so it can be kept private.
            key_path = output.with_name(f"{output.name}.key.csv")
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(("rater_clip_id", "clip_id"))
                    writer.writerows(key)
            except BaseException:
                key_path.unlink()
                raise
        else:
            with (output / "clips.csv").open("x", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, FIELDS)
                writer.writeheader()
                for clip, participant, original, wav in rows:
                    writer.writerow({
                        "participant_id": participant, "clip_id": clip.id, "condition": clip.condition_code,
                        "task": clip.task_type, "task_position": clip.task_position, "prompt_id": clip.prompt_id,
                        "attempt": clip.attempt, "suggested_condition": clip.suggested_condition,
                        "order_position": clip.order_position, "original_path": original, "wav_path": wav,
                        **clip.fast_qc,
                    })
    except BaseException:
        shutil.rmtree(output)
        raise


if __name__ == "__main__":
    main()
