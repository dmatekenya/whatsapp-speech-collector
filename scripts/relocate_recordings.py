"""Move recordings stored before their sender was registered into the participant's folder.

    uv run python scripts/relocate_recordings.py --dry-run
    uv run python scripts/relocate_recordings.py

_unassigned/UNASSIGNED_R000001.wav -> P0001/P0001_R000001.wav (and the .ogg).
Each recording is moved and updated in its own transaction; if the database
update fails, its files are moved back. Recordings from senders who still
aren't registered stay where they are.
"""

import argparse
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import select

from src.logging_config import setup_logging
from src.registry.participants import find_participant
from src.storage.audio import get_audio_storage, recording_filename, resolve_folder
from src.storage.db import get_sync_sessionmaker
from src.storage.models import InboundMessage, Recording

logger = logging.getLogger("relocate_recordings")


@dataclass
class RelocationResult:
    moved: int = 0
    unregistered: int = 0
    failed: int = 0


# ============================================================
# Relocation
# ============================================================
def _target(participant_id: str, recording_id: int, folder: str, current: str) -> str:
    ext = PurePosixPath(current).suffix
    return f"{folder}/{recording_filename(participant_id, recording_id, ext)}"


def relocate_recordings(dry_run: bool = False) -> RelocationResult:
    result = RelocationResult()
    storage = get_audio_storage()
    session_factory = get_sync_sessionmaker()

    with session_factory() as session:
        candidates = session.execute(
            select(Recording.id, InboundMessage.sender)
            .join(InboundMessage, Recording.inbound_message_id == InboundMessage.id)
            .where(Recording.participant_id.is_(None), Recording.status == "stored")
            .order_by(Recording.id)
        ).all()

    for recording_id, sender in candidates:
        with session_factory() as session:
            recording = session.get(Recording, recording_id, with_for_update=True)
            if recording is None or recording.participant_id is not None:
                continue
            # Registered at all (any status): the audio belongs to them.
            participant = find_participant(session, sender)
            if participant is None:
                result.unregistered += 1
                continue

            participant_id = participant.participant_id
            folder = resolve_folder(sender, participant_id)
            moves = [
                (path, _target(participant_id, recording.id, folder, path))
                for path in (recording.original_path, recording.wav_path)
                if path
            ]
            if dry_run:
                for old, new in moves:
                    logger.info("Would move recording id=%s: %s -> %s", recording.id, old, new)
                result.moved += 1
                continue

            done: list[tuple[str, str]] = []
            try:
                for old, new in moves:
                    storage.move(old, new)
                    done.append((old, new))
                new_paths = dict(moves)
                recording.participant_id = participant_id
                recording.folder = folder
                recording.original_path = new_paths.get(recording.original_path, recording.original_path)
                recording.wav_path = new_paths.get(recording.wav_path, recording.wav_path)
                message = session.get(InboundMessage, recording.inbound_message_id)
                if message is not None and message.participant_id is None:
                    message.participant_id = participant_id
                session.commit()
            except Exception as exc:
                session.rollback()
                for old, new in reversed(done):
                    storage.move(new, old)
                logger.error("Could not relocate recording id=%s: %s", recording_id, exc)
                result.failed += 1
                continue
            for old, new in moves:
                logger.info("Moved recording id=%s: %s -> %s", recording.id, old, new)
            result.moved += 1

    logger.info(
        "%s%s recording(s) relocated, %s from unregistered senders left in place, %s failed",
        "[dry run] would be " if dry_run else "",
        result.moved,
        result.unregistered,
        result.failed,
    )
    return result


# ============================================================
# CLI
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would move, move nothing")
    args = parser.parse_args()

    setup_logging()
    relocate_recordings(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
