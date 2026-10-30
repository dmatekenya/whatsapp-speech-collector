"""One-off: delete Twilio media that should already have been deleted.

    uv run python -m scripts.delete_channel_media --dry-run
    uv run python -m scripts.delete_channel_media

Covers (a) stored recordings whose media wasn't deleted (twilio_media_deleted
is false), and (b) media from senders who weren't registered, which is never
downloaded. Each success is recorded (inbound_messages.media_deleted_at, and
recordings.twilio_media_deleted for (a)); media Twilio no longer has counts
as deleted. Failures are listed with Twilio's error and can be re-run.
Deletion can't be undone: run --dry-run first.
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.channels.base import ChannelAdapter, MediaDeleteError
from src.channels.factory import get_channel
from src.config import get_settings
from src.logging_config import setup_logging
from src.storage.db import get_sync_sessionmaker
from src.storage.models import InboundMessage, Recording
from src.workers.jobs import has_media, record_media_deleted

logger = logging.getLogger("delete_channel_media")


@dataclass
class Candidate:
    message_id: int
    message_sid: str
    kind: str  # "stored recording" or "unregistered sender"
    recording_id: int | None


@dataclass
class DeleteResult:
    candidates: list[Candidate] = field(default_factory=list)
    deleted: list[Candidate] = field(default_factory=list)
    failed: list[tuple[Candidate, str]] = field(default_factory=list)


# ==========
# CANDIDATES
# ==========
def find_candidates(session: Session) -> list[Candidate]:
    stored = session.execute(
        select(InboundMessage.id, InboundMessage.message_sid, Recording.id)
        .join(Recording, Recording.inbound_message_id == InboundMessage.id)
        .where(Recording.status == "stored", Recording.twilio_media_deleted.is_(False))
        .order_by(InboundMessage.id)
    ).all()
    unregistered = session.scalars(
        select(InboundMessage)
        .where(InboundMessage.status == "unregistered", InboundMessage.media_deleted_at.is_(None))
        .order_by(InboundMessage.id)
    ).all()
    return [Candidate(m, sid, "stored recording", r) for m, sid, r in stored] + [
        Candidate(m.id, m.message_sid, "unregistered sender", None) for m in unregistered if has_media(m)
    ]


# ========
# DELETING
# ========
def delete_pending_media(
    session_factory: sessionmaker[Session], channel: ChannelAdapter, dry_run: bool = False
) -> DeleteResult:
    result = DeleteResult()
    with session_factory() as session:
        result.candidates = find_candidates(session)
    if dry_run:
        return result

    for candidate in result.candidates:
        with session_factory() as session:
            message = session.get(InboundMessage, candidate.message_id, with_for_update=True)
            recording = session.get(Recording, candidate.recording_id) if candidate.recording_id else None
            try:
                channel.delete_media(channel.parse_inbound(message.raw_payload))
            except MediaDeleteError as exc:
                result.failed.append((candidate, str(exc)))
                continue
            except Exception as exc:
                result.failed.append((candidate, type(exc).__name__))
                continue
            record_media_deleted(session, message, recording)
            result.deleted.append(candidate)
    return result


# ===
# CLI
# ===
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    args = parser.parse_args()

    setup_logging()
    if not get_settings().twilio_delete_media:
        logger.warning("TWILIO_DELETE_MEDIA is off; this one-off deletes anyway")
    result = delete_pending_media(get_sync_sessionmaker(), get_channel(), dry_run=args.dry_run)
    prefix = "[dry run] would delete" if args.dry_run else "to delete"
    for candidate in result.candidates:
        what = f"recording id={candidate.recording_id}" if candidate.recording_id else "never downloaded"
        logger.info("%s: message id=%s (%s, %s)", prefix, candidate.message_id, candidate.kind, what)
    for candidate, error in result.failed:
        logger.error("Failed: message id=%s: %s", candidate.message_id, error)
    kinds = {kind: sum(c.kind == kind for c in result.candidates) for kind in ("stored recording", "unregistered sender")}
    logger.info(
        "%s media: %s stored recording(s), %s unregistered sender(s)%s",
        "Found" if args.dry_run else "Processed",
        kinds["stored recording"],
        kinds["unregistered sender"],
        "" if args.dry_run else f"; deleted {len(result.deleted)}, failed {len(result.failed)}",
    )
    if result.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
