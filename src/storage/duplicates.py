from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.storage.models import InboundMessage, Recording


@dataclass(frozen=True)
class LinkChange:
    recording_id: int
    old: int | None
    new: int | None


def relink_duplicates(session: Session, content_sha256: str) -> list[LinkChange]:
    """Point every stored recording with this hash at the original; return what changed.

    The original is the recording whose inbound message was received first
    (received_at, then inbound_messages.id as a tie-breaker), not the lowest
    recording id: backlog processing can give a later message a lower id.
    Doesn't commit.
    """
    group = session.scalars(
        select(Recording)
        .join(InboundMessage, Recording.inbound_message_id == InboundMessage.id)
        .where(Recording.content_sha256 == content_sha256, Recording.status == "stored")
        .order_by(InboundMessage.received_at, InboundMessage.id)
        # Lock the group so two workers storing copies can't interleave.
        .with_for_update(of=Recording)
    ).all()
    if not group:
        return []

    original_id = group[0].id
    changes = []
    for recording in group:
        expected = None if recording.id == original_id else original_id
        if recording.duplicate_of_recording_id != expected:
            changes.append(LinkChange(recording.id, recording.duplicate_of_recording_id, expected))
            recording.duplicate_of_recording_id = expected
    return changes
