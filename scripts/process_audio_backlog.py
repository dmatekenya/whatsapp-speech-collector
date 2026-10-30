"""Enqueue process_inbound for audio messages that have no stored recording.

For voice notes received before audio storage existed, or whose recording failed:

    uv run python -m scripts.process_audio_backlog --dry-run
    uv run python -m scripts.process_audio_backlog
    uv run python -m scripts.process_audio_backlog --message-id 42
    uv run python -m scripts.process_audio_backlog --message-id SM21e3b45d0a61e677faf53ee91e5b0018

With --backfill-hashes it enqueues nothing; instead it computes content hashes
for stored recordings from their original files and sets duplicate links:

    uv run python -m scripts.process_audio_backlog --backfill-hashes --dry-run
    uv run python -m scripts.process_audio_backlog --backfill-hashes
"""

import argparse
import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import exists, select

from src.logging_config import setup_logging
from src.storage.audio import get_audio_storage
from src.storage.db import get_sync_sessionmaker
from src.storage.duplicates import relink_duplicates
from src.storage.models import InboundMessage, Recording
from src.storage.queue import enqueue_inbound, get_inbound_queue

logger = logging.getLogger("process_audio_backlog")


# =======
# BACKLOG
# =======
def process_audio_backlog(message_id: str | None = None, dry_run: bool = False) -> int:
    stored = exists().where(
        Recording.inbound_message_id == InboundMessage.id, Recording.status == "stored"
    )
    query = (
        select(InboundMessage)
        # Media deleted from the channel can't be downloaded any more.
        .where(
            InboundMessage.message_type == "audio",
            ~stored,
            InboundMessage.media_deleted_at.is_(None),
        )
        .order_by(InboundMessage.id)
    )
    if message_id is not None:
        # Accept either the inbound_messages.id or the channel's message SID.
        query = query.where(
            InboundMessage.id == int(message_id)
            if message_id.isdigit()
            else InboundMessage.message_sid == message_id
        )

    queue = get_inbound_queue()
    enqueued = 0
    with get_sync_sessionmaker()() as session:
        messages = session.scalars(query).all()
        for message in messages:
            if dry_run:
                logger.info("Would enqueue id=%s status=%s", message.id, message.status)
                continue
            enqueue_inbound(queue, message.message_sid)
            message.status = "queued"
            session.commit()
            enqueued += 1
            logger.info("Enqueued id=%s", message.id)

    logger.info("Found %s audio message(s) without a stored recording, enqueued %s", len(messages), enqueued)
    return enqueued


# =============
# HASH BACKFILL
# =============
@dataclass
class BackfillResult:
    hashed: int = 0
    missing_files: int = 0
    links_changed: int = 0


def backfill_hashes(dry_run: bool = False) -> BackfillResult:
    """Hash stored recordings that have no hash, then set duplicate links by arrival order.

    Idempotent: a second run hashes nothing and changes no links.
    """
    result = BackfillResult()
    storage = get_audio_storage()

    with get_sync_sessionmaker()() as session:
        recordings = session.scalars(
            select(Recording).where(Recording.status == "stored").order_by(Recording.id)
        ).all()

        for recording in recordings:
            if recording.content_sha256 is not None:
                continue
            if not recording.original_path or not storage.exists(recording.original_path):
                logger.warning("Recording id=%s: original file missing, not hashed", recording.id)
                result.missing_files += 1
                continue
            recording.content_sha256 = hashlib.sha256(
                storage.read(recording.original_path)
            ).hexdigest()
            result.hashed += 1

        # Within each hash, the recording whose message was received first is
        # the original; every other one points to it. Also corrects links set
        # by earlier versions, which went by recording id.
        session.flush()
        hashes = sorted({r.content_sha256 for r in recordings if r.content_sha256 is not None})
        for content_sha256 in hashes:
            for change in relink_duplicates(session, content_sha256):
                logger.info(
                    "Recording id=%s duplicate_of: %s -> %s",
                    change.recording_id,
                    change.old,
                    change.new,
                )
                result.links_changed += 1

        if dry_run:
            session.rollback()
        else:
            session.commit()

    logger.info(
        "%sHashed %s recording(s), %s missing file(s), %s duplicate link(s) changed",
        "[dry run] " if dry_run else "",
        result.hashed,
        result.missing_files,
        result.links_changed,
    )
    return result


# ===
# CLI
# ===
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--message-id", help="only this message (inbound_messages.id or message SID)")
    parser.add_argument(
        "--backfill-hashes",
        action="store_true",
        help="hash stored recordings and set duplicate links instead of enqueuing",
    )
    parser.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    args = parser.parse_args()

    setup_logging()
    if args.backfill_hashes:
        if args.message_id is not None:
            parser.error("--message-id can't be combined with --backfill-hashes")
        backfill_hashes(dry_run=args.dry_run)
    else:
        process_audio_backlog(args.message_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
