from dataclasses import dataclass

from sqlalchemy import and_, exists, select, text, tuple_
from sqlalchemy.orm import Session

from src.registry.enrollments import EXPERIMENT, MAIN
from src.storage.models import Enrollment, InboundMessage, Recording, main_recordings

EXPERIMENT_DUPLICATE = "duplicate_of_experiment_clip"


@dataclass(frozen=True)
class LinkChange:
    recording_id: int
    old: int | None
    new: int | None


# ==================
# STUDY DUPLICATES
# ==================
def lock_recording_hash(session: Session, content_sha256: str) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"recording-hash:{content_sha256}"})


def flag_experiment_match(session: Session, recording: Recording, inbound: InboundMessage) -> None:
    """Flag a new main recording without linking it to an experiment recording."""
    match = select(Recording.id).join(Enrollment).join(
        InboundMessage, InboundMessage.id == Recording.inbound_message_id,
    ).where(
        Enrollment.study == EXPERIMENT,
        Recording.status == "stored",
        Recording.content_sha256 == recording.content_sha256,
        tuple_(InboundMessage.received_at, InboundMessage.id) < (inbound.received_at, inbound.id),
    )
    if session.scalar(select(match.exists())):
        recording.qc_status = "rejected"
        recording.qc_reason = EXPERIMENT_DUPLICATE


def relink_duplicates(session: Session, content_sha256: str, *, study: str = MAIN) -> list[LinkChange]:
    """Point matches within the same study at the earliest received recording."""
    scope = (
        and_(exists().where(main_recordings.c.id == Recording.id),
             Recording.qc_reason.is_distinct_from(EXPERIMENT_DUPLICATE)) if study == MAIN
        else exists().where(Enrollment.id == Recording.enrollment_id, Enrollment.study == study)
    )
    group = session.scalars(
        select(Recording)
        .join(InboundMessage, Recording.inbound_message_id == InboundMessage.id)
        .where(Recording.content_sha256 == content_sha256, Recording.status == "stored", scope)
        .order_by(InboundMessage.received_at, InboundMessage.id)
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
