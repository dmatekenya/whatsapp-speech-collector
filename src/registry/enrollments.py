"""Study enrollment transitions and receipt-time attribution."""

from datetime import UTC, datetime
from functools import lru_cache

import yaml
from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session

from src.config import PROJECT_ROOT
from src.conversation.locks import lock_conversation
from src.storage.models import Assignment, ConversationState, Enrollment, EnrollmentActivation, Participant, TaskSession

MAIN = "main"
EXPERIMENT = "qc_calibration"


# =======
# LOOKUPS
# =======
@lru_cache
def lifetime_studies() -> tuple[str, ...]:
    with (PROJECT_ROOT / "config/participants.yaml").open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    return tuple(key for key, spec in config["studies"].items() if spec["counts_toward_lifetime"])


def enrollment_status(participant_status: str) -> str:
    return "paused" if participant_status == "inactive" else participant_status


def active_enrollment(db: Session, participant_id: str, *, received_at: datetime | None = None) -> Enrollment | None:
    query = select(Enrollment).where(
        Enrollment.participant_id == participant_id, Enrollment.status == "active",
    )
    if received_at is not None:
        query = query.join(EnrollmentActivation).where(
            EnrollmentActivation.deactivated_at.is_(None),
            EnrollmentActivation.activated_at <= received_at,
        )
    return db.scalar(query)


def received_enrollment(db: Session, participant_id: str, received_at: datetime) -> Enrollment | None:
    return db.scalar(select(Enrollment).join(EnrollmentActivation).where(
        Enrollment.participant_id == participant_id,
        EnrollmentActivation.activated_at <= received_at,
        or_(EnrollmentActivation.deactivated_at.is_(None), EnrollmentActivation.deactivated_at > received_at),
    ).order_by(EnrollmentActivation.activated_at.desc(), EnrollmentActivation.id.desc()).limit(1))


# ===========
# TRANSITIONS
# ===========
def pause_main_session(db: Session, participant_id: str, now: datetime) -> None:
    db.execute(update(Assignment).where(
        Assignment.participant_id == participant_id, Assignment.status == "sent",
    ).values(status="expired"))
    db.execute(update(TaskSession).where(
        TaskSession.participant_id == participant_id, TaskSession.status == "active",
    ).values(status="paused", ended_at=now))
    state = db.get(ConversationState, participant_id)
    if state is not None:
        state.state = "idle"
        state.context = {**state.context, "session_id": None, "offered_task_type": None, "previous_state": None}


def activate(db: Session, participant: Participant, enrollment: Enrollment) -> None:
    lock_conversation(db, participant.phone_number)
    current = active_enrollment(db, participant.participant_id)
    if current is not None and current.id == enrollment.id:
        return
    # Close and open intervals at the same instant, even though the unique
    # index requires the two status changes to be flushed in order.
    db.execute(text("SELECT set_config('collector.enrollment_transition_at', clock_timestamp()::text, true)"))
    if current is not None:
        if current.study == MAIN:
            pause_main_session(db, participant.participant_id, datetime.now(UTC))
        current.status = "paused"
        db.flush()  # close the previous enrollment before inserting or activating the next
    enrollment.status = "active"
    db.add(enrollment)
    db.flush()
    db.execute(text("SELECT set_config('collector.enrollment_transition_at', '', true)"))
