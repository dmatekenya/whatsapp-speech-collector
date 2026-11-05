"""Task bank: which prompt each participant gets, and what happened to it.

Every function takes a SQLAlchemy Session and never commits; the caller owns
the transaction. choose_next_prompt() locks the chosen prompt row until that
transaction ends, so call create_assignment() in the same transaction and
commit promptly. Functions that depend on the clock take ``now`` (default:
the current UTC time), so tests and the simulation can use a fake clock.

Coverage of a prompt = its assignments that are accepted, recorded, or sent
and not yet expired. A prompt is available to a participant while its
coverage is below speakers_per_prompt for its type and that participant has
never had it before. Rejected, expired and skipped assignments free the slot.

Unprompted task types (free_speech) have no prompts and no coverage: each
position in their sessions is an assignment with no prompt_id, an invitation
to talk about anything. Everything else (session size, expiry, retakes,
answers, minutes and targets) works the same for every type.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from src.storage.models import Assignment, OutboundMessage, Participant, Prompt, Recording, TaskSession
from src.tasks.config import CollectionConfig, get_collection_config

# Assignments that hold a slot regardless of time ("sent" holds one until it expires).
HOLDING_STATUSES = ("recorded", "accepted")
# Assignments whose recording counts toward a participant's minutes.
# TODO(QC): once recordings have a QC status, count only those that passed.
COUNTED_STATUSES = ("recorded", "accepted")
ENDED_SESSION_STATUSES = ("completed", "paused", "abandoned")
# A late or repeated answer is still accepted for these.
ANSWERABLE_STATUSES = ("sent", "expired", "recorded")


class TaskBankError(RuntimeError):
    pass


class ActiveSessionError(TaskBankError):
    """The participant already has an active session."""


class AssignmentClosedError(TaskBankError):
    """The assignment can't take this change in its current status."""


class RetakeLimitError(TaskBankError):
    """The assignment has used up max_retakes_per_prompt."""


def utcnow() -> datetime:
    return datetime.now(UTC)


# =========================
# COVERAGE AND AVAILABILITY
# =========================
def _holds_slot(now: datetime) -> ColumnElement[bool]:
    return or_(
        Assignment.status.in_(HOLDING_STATUSES),
        and_(Assignment.status == "sent", Assignment.expires_at > now),
    )


def _coverage(now: datetime):  # noqa: ANN202 - a correlated scalar subquery
    return (
        select(func.count(Assignment.id))
        .where(Assignment.prompt_id == Prompt.prompt_id, _holds_slot(now))
        .correlate(Prompt)
        .scalar_subquery()
    )


def prompt_coverage(db: Session, prompt_id: str, now: datetime | None = None) -> int:
    now = now or utcnow()
    return db.scalar(
        select(func.count(Assignment.id)).where(Assignment.prompt_id == prompt_id, _holds_slot(now))
    )


def _available(participant_id: str, task_type: str, limit: int, now: datetime) -> list[ColumnElement[bool]]:
    already_had = exists().where(
        Assignment.participant_id == participant_id, Assignment.prompt_id == Prompt.prompt_id
    )
    return [
        Prompt.prompt_type == task_type,
        Prompt.active.is_(True),
        ~already_had,
        _coverage(now) < limit,
    ]


def _limit(task_type: str, config: CollectionConfig) -> int:
    if not config.is_prompted(task_type):  # also raises for an unknown type
        raise ValueError(f"{task_type!r} is unprompted: it has no prompts to choose")
    return config.speakers_per_prompt[task_type]


def has_available_prompt(
    db: Session,
    participant_id: str,
    task_type: str,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> bool:
    config = config or get_collection_config()
    if not config.is_prompted(task_type):
        return True  # an invitation to talk is always available
    now = now or utcnow()
    filters = _available(participant_id, task_type, _limit(task_type, config), now)
    return db.scalar(select(exists().where(*filters))) is True


def expire_stale_assignments(db: Session, now: datetime | None = None) -> int:
    """Sent assignments past expires_at become expired, freeing their slots."""
    now = now or utcnow()
    result = db.execute(
        update(Assignment)
        .where(Assignment.status == "sent", Assignment.expires_at <= now)
        .values(status="expired")
        # Keep already-loaded Assignment objects in this session up to date.
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


def choose_next_prompt(
    db: Session,
    participant_id: str,
    task_type: str,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> Prompt | None:
    """The next prompt of this type for this participant, or None.

    Least-covered first, then prompts without length flags, then random. The
    returned prompt's row stays locked until the caller's transaction ends.
    """
    config = config or get_collection_config()
    now = now or utcnow()
    limit = _limit(task_type, config)
    expire_stale_assignments(db, now)

    tried: list[int] = []
    while True:
        query = (
            select(Prompt)
            .where(*_available(participant_id, task_type, limit, now))
            .order_by(_coverage(now).asc(), Prompt.flags.has_key("length").asc(), func.random())
            .limit(1)
            # Rows another transaction is assigning right now are skipped, not waited for.
            .with_for_update(of=Prompt, skip_locked=True)
        )
        if tried:
            query = query.where(Prompt.id.not_in(tried))
        prompt = db.scalars(query).first()
        if prompt is None:
            return None
        # Recount in a new statement while holding the lock: unlike the query
        # above, this sees assignments committed by whoever held the lock
        # before us, so the last slot can't be handed out twice.
        if prompt_coverage(db, prompt.prompt_id, now) < limit:
            return prompt
        tried.append(prompt.id)


# ====================
# CHOOSING A TASK TYPE
# ====================
def minutes_recorded(db: Session, participant_id: str) -> dict[str, float]:
    """Recorded minutes per task type: the current recording of each answered
    assignment, excluding recordings flagged as duplicates. The one definition
    of progress toward targets."""
    totals = dict.fromkeys(get_collection_config().task_types, 0.0)
    rows = db.execute(
        select(Assignment.prompt_type, func.coalesce(func.sum(Recording.duration_s), 0.0))
        .join(Recording, Recording.id == Assignment.recording_id)
        .where(
            Assignment.participant_id == participant_id,
            Assignment.status.in_(COUNTED_STATUSES),
            Recording.status == "stored",
            Recording.duplicate_of_recording_id.is_(None),
            # Clips rejected as failed attempts (or later by QC) never count.
            Recording.qc_status != "rejected",
        )
        .group_by(Assignment.prompt_type)
    )
    for task_type, seconds in rows:
        totals[task_type] = float(seconds) / 60
    return totals


def choose_task_type(
    db: Session,
    participant_id: str,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> str | None:
    """The task type with the largest share of its target still to record.

    Types whose target is met, or with no prompt available to this
    participant, are skipped. None only when every type's target is met, no
    prompts are available for the unmet ones, or the participant isn't
    active. Targets are per type (config.targets_for); the total is only
    their sum, so a participant is done when every type is.
    """
    config = config or get_collection_config()
    now = now or utcnow()
    participant = db.scalar(select(Participant).where(Participant.participant_id == participant_id))
    if participant is None or participant.status != "active":
        return None

    targets = config.targets_for(participant.attributes)
    done = minutes_recorded(db, participant_id)
    expire_stale_assignments(db, now)
    candidates = []
    for order, task_type in enumerate(config.task_types):
        target = targets.get(task_type, 0.0)
        remaining = target - done.get(task_type, 0.0)
        if target > 0 and remaining > 0:
            # Largest share remaining first; ties keep config order.
            candidates.append((-remaining / target, order, task_type))
    for _, _, task_type in sorted(candidates):
        if has_available_prompt(db, participant_id, task_type, now, config):
            return task_type
    return None


# ========
# SESSIONS
# ========
def get_active_session(db: Session, participant_id: str) -> TaskSession | None:
    return db.scalar(
        select(TaskSession).where(
            TaskSession.participant_id == participant_id, TaskSession.status == "active"
        )
    )


def start_session(
    db: Session,
    participant_id: str,
    task_type: str,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> TaskSession:
    config = config or get_collection_config()
    config.task_type(task_type)  # raises for an unknown type
    if get_active_session(db, participant_id) is not None:
        raise ActiveSessionError(f"{participant_id} already has an active session")
    task_session = TaskSession(
        participant_id=participant_id,
        task_type=task_type,
        status="active",
        size=config.session_size,
        started_at=now or utcnow(),
    )
    db.add(task_session)
    db.flush()
    return task_session


def end_session(
    db: Session, session_id: int, status: str = "completed", now: datetime | None = None
) -> TaskSession:
    if status not in ENDED_SESSION_STATUSES:
        raise ValueError(f"status must be one of {ENDED_SESSION_STATUSES}")
    task_session = db.get(TaskSession, session_id, with_for_update=True)
    if task_session is None:
        raise LookupError(f"Session {session_id} not found")
    if task_session.status != "active":
        raise TaskBankError(f"Session {session_id} is already {task_session.status}")
    task_session.status = status
    task_session.ended_at = now or utcnow()
    db.flush()
    return task_session


# ===========
# ASSIGNMENTS
# ===========
def create_assignment(
    db: Session,
    participant_id: str,
    prompt: Prompt,
    task_session: TaskSession,
    position: int,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> Assignment:
    """Assign a prompt (from choose_next_prompt, same transaction), snapshotting its text."""
    config = config or get_collection_config()
    now = now or utcnow()
    if task_session.participant_id != participant_id or task_session.status != "active":
        raise TaskBankError("the session isn't this participant's active session")
    if prompt.prompt_type != task_session.task_type:
        raise TaskBankError(f"{prompt.prompt_id} isn't a {task_session.task_type} prompt")
    if not 1 <= position <= task_session.size:
        raise ValueError(f"position must be between 1 and {task_session.size}")
    assignment = Assignment(
        participant_id=participant_id,
        prompt_id=prompt.prompt_id,
        prompt_type=prompt.prompt_type,
        session_id=task_session.id,
        position=position,
        prompt_text_snapshot=prompt.text,  # None for images
        status="sent",
        retake_count=0,
        expires_at=now + timedelta(hours=config.assignment_expiry_hours),
    )
    db.add(assignment)
    db.flush()
    return assignment


def create_invitation(
    db: Session,
    task_session: TaskSession,
    position: int,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> Assignment:
    """An assignment with no prompt, for an unprompted task type (free_speech)."""
    config = config or get_collection_config()
    now = now or utcnow()
    if task_session.status != "active":
        raise TaskBankError("the session isn't active")
    if config.is_prompted(task_session.task_type):
        raise TaskBankError(f"{task_session.task_type} is prompted: use create_assignment")
    if not 1 <= position <= task_session.size:
        raise ValueError(f"position must be between 1 and {task_session.size}")
    assignment = Assignment(
        participant_id=task_session.participant_id,
        prompt_id=None,
        prompt_type=task_session.task_type,
        session_id=task_session.id,
        position=position,
        status="sent",
        retake_count=0,
        expires_at=now + timedelta(hours=config.assignment_expiry_hours),
    )
    db.add(assignment)
    db.flush()
    return assignment


def next_position(db: Session, task_session: TaskSession) -> int | None:
    """The next free position in the session, or None if it's full."""
    used = db.scalar(select(func.count(Assignment.id)).where(Assignment.session_id == task_session.id))
    return used + 1 if used < task_session.size else None


def assign_next_prompt(
    db: Session,
    task_session: TaskSession,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> Assignment | None:
    """choose_next_prompt + create_assignment for the session's next position
    (an invitation, with no prompt, for unprompted types).

    None if the session is full or no prompt is available.
    """
    config = config or get_collection_config()
    position = next_position(db, task_session)
    if position is None:
        return None
    if not config.is_prompted(task_session.task_type):
        return create_invitation(db, task_session, position, now, config)
    prompt = choose_next_prompt(db, task_session.participant_id, task_session.task_type, now, config)
    if prompt is None:
        return None
    return create_assignment(db, task_session.participant_id, prompt, task_session, position, now, config)


def mark_sent(
    db: Session,
    assignment_id: int,
    outbound_message_sid: str,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> Assignment:
    """Record the outbound message; the expiry clock restarts from the send time."""
    config = config or get_collection_config()
    now = now or utcnow()
    assignment = db.get(Assignment, assignment_id, with_for_update=True)
    if assignment is None:
        raise LookupError(f"Assignment {assignment_id} not found")
    if assignment.status != "sent":
        raise AssignmentClosedError(f"Assignment {assignment_id} is {assignment.status}")
    assignment.outbound_message_sid = outbound_message_sid
    assignment.sent_at = now
    assignment.expires_at = now + timedelta(hours=config.assignment_expiry_hours)
    db.flush()
    return assignment


def find_open_assignment(
    db: Session,
    participant_id: str,
    replied_to_message_sid: str | None = None,
    channel: str | None = None,
) -> Assignment | None:
    """The assignment an incoming answer belongs to.

    A WhatsApp reply to a prompt message binds to that prompt's assignment
    (even an older one, whatever its status). With a channel, only a prompt
    sent on that channel matches (outbound_messages.provider_message_id), so
    a reply on one number never binds to a prompt sent from the other.
    Otherwise: the most recently sent, still open assignment in the
    participant's active session.
    """
    if replied_to_message_sid:
        query = select(Assignment).where(
            Assignment.participant_id == participant_id,
            Assignment.outbound_message_sid == replied_to_message_sid,
        )
        if channel is not None:
            query = query.where(
                select(OutboundMessage.id)
                .where(
                    OutboundMessage.assignment_id == Assignment.id,
                    OutboundMessage.channel == channel,
                    OutboundMessage.provider_message_id == replied_to_message_sid,
                )
                .exists()
            )
        assignment = db.scalar(query)
        if assignment is not None:
            return assignment
    task_session = get_active_session(db, participant_id)
    if task_session is None:
        return None
    return db.scalar(
        select(Assignment)
        .where(
            Assignment.session_id == task_session.id,
            Assignment.status == "sent",
            Assignment.sent_at.is_not(None),
        )
        .order_by(Assignment.sent_at.desc(), Assignment.position.desc())
        .limit(1)
    )


def record_response(
    db: Session,
    assignment_id: int,
    recording_id: int,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> Assignment:
    """Link a recording to its assignment (both ways) and mark it recorded.

    A second recording for the same assignment is a retake, allowed up to
    max_retakes_per_prompt; the newest recording is the one that counts.
    """
    config = config or get_collection_config()
    assignment = db.get(Assignment, assignment_id, with_for_update=True)
    recording = db.get(Recording, recording_id, with_for_update=True)
    if assignment is None or recording is None:
        raise LookupError("assignment or recording not found")
    if assignment.status not in ANSWERABLE_STATUSES:
        raise AssignmentClosedError(f"Assignment {assignment_id} is {assignment.status}")
    if recording.participant_id not in (None, assignment.participant_id):
        raise TaskBankError("the recording belongs to a different participant")
    if assignment.recording_id is not None and assignment.recording_id != recording_id:
        if assignment.retake_count >= config.max_retakes_per_prompt:
            raise RetakeLimitError(f"Assignment {assignment_id} has no retakes left")
        assignment.retake_count += 1
    assignment.status = "recorded"
    assignment.answered_at = now or utcnow()
    assignment.recording_id = recording.id
    recording.assignment_id = assignment.id
    db.flush()
    return assignment


def session_type_target_reached(
    db: Session, participant_id: str, session_id: int, config: CollectionConfig | None = None
) -> bool:
    """Whether the participant has met their target for this session's task type."""
    config = config or get_collection_config()
    task_session = db.get(TaskSession, session_id)
    if task_session is None or task_session.participant_id != participant_id:
        raise LookupError(f"Session {session_id} not found for {participant_id}")
    participant = db.scalar(select(Participant).where(Participant.participant_id == participant_id))
    targets = config.targets_for(participant.attributes if participant else None)
    done = minutes_recorded(db, participant_id)
    return done.get(task_session.task_type, 0.0) >= targets.get(task_session.task_type, 0.0)


@dataclass(frozen=True)
class ResponseOutcome:
    assignment: Assignment
    # The session ended because its task type's target was met.
    session_ended: bool
    # When it ended: the type to offer next (None: nothing left to record).
    next_task_type: str | None


def handle_response(
    db: Session,
    assignment_id: int,
    recording_id: int,
    now: datetime | None = None,
    config: CollectionConfig | None = None,
) -> ResponseOutcome:
    """The assignment flow after a recording arrives: record it, then end the
    session early (completed) if its task type's target is now met, and pick
    the next task type for the conversation engine to offer."""
    config = config or get_collection_config()
    now = now or utcnow()
    assignment = record_response(db, assignment_id, recording_id, now, config)
    task_session = db.get(TaskSession, assignment.session_id)
    if (
        task_session is None
        or task_session.status != "active"
        or not session_type_target_reached(db, assignment.participant_id, task_session.id, config)
    ):
        return ResponseOutcome(assignment, session_ended=False, next_task_type=None)
    end_session(db, task_session.id, "completed", now)
    next_task_type = choose_task_type(db, assignment.participant_id, now, config)
    return ResponseOutcome(assignment, session_ended=True, next_task_type=next_task_type)


def record_failed_attempt(
    db: Session, assignment_id: int, recording_id: int, reason: str
) -> Recording:
    """A clip sent for this assignment that can't be used (too_short, too_long,
    empty, or not_recorded: forwarded).

    It's linked to the assignment, so it isn't an unassigned recording, and
    rejected, so it never counts toward minutes. assignment.recording_id keeps pointing at the
    newest clip that wasn't rejected (if any).
    """
    assignment = db.get(Assignment, assignment_id, with_for_update=True)
    recording = db.get(Recording, recording_id, with_for_update=True)
    if assignment is None or recording is None:
        raise LookupError("assignment or recording not found")
    if recording.participant_id not in (None, assignment.participant_id):
        raise TaskBankError("the recording belongs to a different participant")
    recording.assignment_id = assignment.id
    recording.qc_status = "rejected"
    recording.qc_reason = reason
    db.flush()
    return recording


def skip_assignment(db: Session, assignment_id: int) -> Assignment:
    """Give up on an unanswered prompt (e.g. too many failed retakes); frees its slot."""
    assignment = db.get(Assignment, assignment_id, with_for_update=True)
    if assignment is None:
        raise LookupError(f"Assignment {assignment_id} not found")
    if assignment.status not in ("sent", "expired"):
        raise AssignmentClosedError(f"Assignment {assignment_id} is {assignment.status}")
    assignment.status = "skipped"
    db.flush()
    return assignment


def set_assignment_outcome(db: Session, assignment_id: int, accepted: bool) -> Assignment:
    """QC result for a recorded assignment. Rejected frees the prompt's slot."""
    assignment = db.get(Assignment, assignment_id, with_for_update=True)
    if assignment is None:
        raise LookupError(f"Assignment {assignment_id} not found")
    if assignment.status not in ("recorded", "accepted", "rejected"):
        raise AssignmentClosedError(f"Assignment {assignment_id} is {assignment.status}")
    assignment.status = "accepted" if accepted else "rejected"
    db.flush()
    return assignment
