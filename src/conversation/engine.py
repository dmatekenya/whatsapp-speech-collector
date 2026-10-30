"""The conversation engine: what the bot says in reply to each inbound message.

respond(message_sid) runs inside process_inbound, after the audio has been
stored. Per turn, in one transaction: take an advisory lock on the sender
(so one participant's messages are handled one at a time), read the state,
decide, write the new state and the reply's outbound_messages rows, commit.
The send_outbound job then sends them. Re-running a turn whose reply exists
just returns that reply's pending rows, so a retried job never replies twice.

States: idle | awaiting_ready | in_session | awaiting_continue | awaiting_stop_confirm.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.config import get_settings
from src.conversation.messages import Catalog, Rendered, get_catalog
from src.conversation.outbound import PromptPart, Reply, emit
from src.logging_config import mask_phone
from src.storage.db import get_sync_sessionmaker
from src.storage.models import (
    Assignment,
    ConversationState,
    InboundMessage,
    OutboundMessage,
    Participant,
    Recording,
    TaskSession,
)
from src.tasks.bank import (
    ANSWERABLE_STATUSES,
    assign_next_prompt,
    choose_task_type,
    end_session,
    expire_stale_assignments,
    find_open_assignment,
    get_active_session,
    handle_response,
    record_failed_attempt,
    minutes_recorded,
    skip_assignment,
    start_session,
)
from src.tasks.config import TOTAL, CollectionConfig, get_collection_config

logger = logging.getLogger(__name__)

UNREGISTERED_REPLY_EVERY = timedelta(hours=24)
# Shorter than this, a voice note is treated as empty.
EMPTY_AUDIO_SECONDS = 0.5
# Catalog problem -> recordings.qc_reason for clips rejected as failed attempts.
FAILED_ATTEMPT_REASONS = {"empty_audio": "empty", "too_short": "too_short", "too_long": "too_long"}
_MEDIA_PROBLEMS = {"image": "image", "video": "video", "document": "document", "other": "other"}


# =======
# HELPERS
# =======
def _kind(message: InboundMessage) -> str:
    """text | audio | image | video | document | other."""
    if message.message_type in ("text", "audio"):
        return message.message_type
    content_type = (message.raw_payload or {}).get("MediaContentType0", "") or ""
    for prefix, kind in (("image/", "image"), ("video/", "video"), ("application/", "document"), ("text/", "document")):
        if content_type.startswith(prefix):
            return kind
    return "other"


def _first_name(participant: Participant) -> str:
    return (participant.name or "").split()[0] if participant.name else "there"


def _minutes(value: float) -> str:
    return f"{value:.1f}"


@dataclass
class Turn:
    db: Session
    message: InboundMessage
    participant: Participant
    state: ConversationState
    recording: Recording | None
    now: datetime

    @property
    def body(self) -> str:
        return str((self.message.raw_payload or {}).get("Body", "") or "")

    @property
    def replied_to(self) -> str | None:
        return (self.message.raw_payload or {}).get("OriginalRepliedMessageSid") or None

    @property
    def context(self) -> dict[str, Any]:
        return self.state.context


# ======
# ENGINE
# ======
class Engine:
    def __init__(self, db: Session, catalog: Catalog, config: CollectionConfig, now: datetime) -> None:
        self.db = db
        self.catalog = catalog
        self.config = config
        self.now = now

    # =========
    # RENDERING
    # =========

    def r(self, key: str, **values: Any) -> Rendered:
        return self.catalog.render(key, **values)

    def _values(self, participant: Participant, **extra: Any) -> dict[str, Any]:
        targets = self.config.targets_for(participant.attributes)
        done = minutes_recorded(self.db, participant.participant_id)
        types = self.config.task_types
        values: dict[str, Any] = {
            "first_name": _first_name(participant),
            "total_done": _minutes(sum(done.values())),
            "total_target": _minutes(targets.get(TOTAL, sum(targets.get(t, 0) for t in types))),
            "session_size": self.config.session_size,
            "min_seconds": f"{self.config.clip_seconds.min:g}",
            "max_seconds": f"{self.config.clip_seconds.max:g}",
            "support_contact": get_settings().support_contact,
        }
        for task_type in types:
            values[f"{task_type}_done"] = _minutes(done.get(task_type, 0.0))
            values[f"{task_type}_target"] = _minutes(targets.get(task_type, 0.0))
        values.update(extra)
        return values

    def task_name(self, task_type: str) -> str:
        return self.catalog.text(f"task_names.{task_type}")

    def _progress(self, participant: Participant) -> Rendered:
        done = minutes_recorded(self.db, participant.participant_id)
        if sum(done.values()) <= 0:
            return self.r("progress.none_yet")
        return self.r("progress.summary", **self._values(participant))

    def _unmet_types(self, participant: Participant) -> list[str]:
        targets = self.config.targets_for(participant.attributes)
        done = minutes_recorded(self.db, participant.participant_id)
        return [t for t in self.config.task_types if done.get(t, 0.0) < targets.get(t, 0.0)]

    def _nothing_to_offer(self, participant: Participant) -> Rendered:
        unmet = self._unmet_types(participant)
        if not unmet:
            return self.r("progress.target_reached", **self._values(participant))
        return self.r("no_prompts_left", task_name=" and ".join(self.task_name(t) for t in unmet))

    def _instructions(self, turn: Turn, task_type: str | None, force: bool = False) -> list[Rendered]:
        """Tips not yet seen (or all of them when force), marking them seen."""
        seen = dict(turn.context.get("instructions_seen") or {})
        parts = []
        for key in ("general", task_type):
            if key and (force or not seen.get(key)):
                parts.append(self.r(f"instructions.{key}", **self._values(turn.participant)))
                seen[key] = True
        if parts:
            parts.append(self.r("instructions.reminder"))
        turn.context["instructions_seen"] = seen
        return parts

    def _prompt(self, assignment: Assignment) -> PromptPart:
        task_session = self.db.get(TaskSession, assignment.session_id)
        size = task_session.size if task_session else self.config.session_size
        if assignment.prompt_type == "image":
            rendered = self.r("prompt.image_caption", n=assignment.position, session_size=size)
            return PromptPart(assignment, rendered, media_prompt_id=assignment.prompt_id)
        rendered = self.r(
            f"prompt.{assignment.prompt_type}",
            n=assignment.position,
            session_size=size,
            text=assignment.prompt_text_snapshot or "",
        )
        return PromptPart(assignment, rendered)

    # =====
    # STATE
    # =====

    def _set(self, turn: Turn, state: str, **context: Any) -> None:
        turn.state.state = state
        turn.context.update(context)

    def _commit_context(self, turn: Turn) -> None:
        # context is edited in place, which SQLAlchemy doesn't see on its own.
        flag_modified(turn.state, "context")

    # =====
    # FLOWS
    # =====

    def offer(self, turn: Turn, reply: Reply | None = None) -> Reply:
        """idle: welcome + progress + today's task + instructions + ready question."""
        reply = reply or Reply()
        participant = turn.participant
        welcome = "welcome.returning" if turn.context.get("welcomed") else "welcome.first_time"
        reply.add(self.r(welcome, first_name=_first_name(participant)))
        turn.context["welcomed"] = True
        task_type = choose_task_type(self.db, participant.participant_id, self.now, self.config)
        if task_type is None:
            if not self._unmet_types(participant):
                reply.add(self._nothing_to_offer(participant))
            else:
                reply.add(self._progress(participant), self._nothing_to_offer(participant))
            self._set(turn, "idle", offered_task_type=None)
            return reply
        reply.add(self._progress(participant), self.r(f"today.{task_type}"))
        reply.add(*self._instructions(turn, task_type))
        reply.add(self.r("ready.ask"))
        self._set(turn, "awaiting_ready", offered_task_type=task_type)
        return reply

    def start(self, turn: Turn, task_type: str | None, reply: Reply | None = None) -> Reply:
        """Start a session of task_type and send its first prompt."""
        reply = reply or Reply()
        participant_id = turn.participant.participant_id
        if task_type is None:
            return self.offer(turn, reply)
        stale = get_active_session(self.db, participant_id)
        if stale is not None:
            end_session(self.db, stale.id, "abandoned", self.now)
        task_session = start_session(self.db, participant_id, task_type, self.now, self.config)
        assignment = assign_next_prompt(self.db, task_session, self.now, self.config)
        if assignment is None:
            end_session(self.db, task_session.id, "abandoned", self.now)
            reply.add(self._nothing_to_offer(turn.participant))
            self._set(turn, "idle", offered_task_type=None)
            return reply
        reply.add(*self._instructions(turn, task_type))
        reply.prompt = self._prompt(assignment)
        self._set(turn, "in_session", offered_task_type=None, session_id=task_session.id)
        return reply

    def next_prompt(self, turn: Turn, reply: Reply, acknowledge: bool = True) -> Reply:
        """After an answer: the next prompt, or wrap up the session."""
        participant_id = turn.participant.participant_id
        task_session = get_active_session(self.db, participant_id)
        if task_session is not None:
            assignment = assign_next_prompt(self.db, task_session, self.now, self.config)
            if assignment is not None:
                reply.prompt = self._prompt(assignment)
                if acknowledge:
                    reply.prompt_prefix = self.r("prompt.ack_prefix")
                self._set(turn, "in_session")
                return reply
            end_session(self.db, task_session.id, "completed", self.now)
        next_type = choose_task_type(self.db, participant_id, self.now, self.config)
        if next_type is not None:
            reply.add(
                self.r(
                    "session.continue_ask",
                    **self._values(turn.participant, next_task_name=self.task_name(next_type)),
                )
            )
            self._set(turn, "awaiting_continue", offered_task_type=next_type)
        else:
            reply.add(self._nothing_to_offer(turn.participant))
            self._set(turn, "idle", offered_task_type=None)
        return reply

    def repeat_prompt(self, turn: Turn, assignment: Assignment, *parts: Rendered) -> Reply:
        reply = Reply(parts=list(parts))
        reply.prompt = self._prompt(assignment)
        return reply

    # ==================
    # AUDIO IN A SESSION
    # ==================

    def _problem(self, recording: Recording | None) -> str | None:
        duration = recording.duration_s if recording is not None else None
        if recording is None or recording.status != "stored" or not duration or duration < EMPTY_AUDIO_SECONDS:
            return "empty_audio"
        if recording.duplicate_of_recording_id is not None:
            return "duplicate"
        if duration < self.config.clip_seconds.min:
            return "too_short"
        if duration > self.config.clip_seconds.max:
            return "too_long"
        return None

    def audio_in_session(self, turn: Turn) -> Reply:
        participant_id = turn.participant.participant_id
        assignment = find_open_assignment(self.db, participant_id, turn.replied_to)
        if assignment is not None and assignment.status not in ANSWERABLE_STATUSES:
            assignment = find_open_assignment(self.db, participant_id)
        if assignment is None:
            # Nothing open (e.g. it expired): close the session, start afresh.
            stale = get_active_session(self.db, participant_id)
            if stale is not None:
                end_session(self.db, stale.id, "abandoned", self.now)
            return self.offer(turn)

        problem = self._problem(turn.recording)
        values = self._values(
            turn.participant,
            duration=f"{(turn.recording.duration_s or 0) if turn.recording else 0:.0f}",
        )
        if problem == "duplicate":
            return self.repeat_prompt(turn, assignment, self.r("problems.duplicate", **values))
        if problem is not None:
            # A failed attempt at this prompt: linked to it, rejected, never
            # free speech and never counted.
            if turn.recording is not None:
                record_failed_attempt(
                    self.db, assignment.id, turn.recording.id, FAILED_ATTEMPT_REASONS[problem]
                )
            if assignment.retake_count >= self.config.max_retakes_per_prompt:
                skip_assignment(self.db, assignment.id)
                reply = Reply(parts=[self.r("problems.retake_limit", **values)])
                return self.next_prompt(turn, reply, acknowledge=False)
            assignment.retake_count += 1
            return self.repeat_prompt(turn, assignment, self.r(f"problems.{problem}", **values))

        outcome = handle_response(self.db, assignment.id, turn.recording.id, self.now, self.config)
        if outcome.session_ended:
            finished = self.task_name(assignment.prompt_type)
            if outcome.next_task_type is not None:
                key_values = self._values(
                    turn.participant,
                    task_name=finished,
                    next_task_name=self.task_name(outcome.next_task_type),
                )
                self._set(turn, "awaiting_continue", offered_task_type=outcome.next_task_type)
                return Reply(parts=[self.r("session.type_target_reached", **key_values)])
            self._set(turn, "idle", offered_task_type=None)
            return Reply(
                parts=[self.r("session.type_target_reached_no_next", **self._values(turn.participant, task_name=finished))]
            )
        # A swipe-reply re-recorded an older prompt: the current one is still
        # open, so send that again rather than a new one.
        still_open = find_open_assignment(self.db, participant_id)
        if still_open is not None and still_open.id != assignment.id:
            reply = self.repeat_prompt(turn, still_open)
            reply.prompt_prefix = self.r("prompt.ack_prefix")
            return reply
        return self.next_prompt(turn, Reply())

    # ========
    # DISPATCH
    # ========

    def global_command(self, turn: Turn) -> Reply | None:
        body = turn.body
        if self.catalog.matches("help", body):
            return Reply(parts=[self.r("help", **self._values(turn.participant))])
        if self.catalog.matches("progress", body):
            return Reply(parts=[self._progress(turn.participant)])
        if self.catalog.matches("instructions", body):
            task_session = get_active_session(self.db, turn.participant.participant_id)
            task_type = task_session.task_type if task_session else turn.context.get("offered_task_type")
            return Reply(parts=self._instructions(turn, task_type, force=True))
        if self.catalog.matches("stop", body):
            self._set(turn, "awaiting_stop_confirm", previous_state=turn.state.state)
            return Reply(parts=[self.r("stop.confirm")])
        return None

    def handle(self, turn: Turn) -> Reply | None:
        kind = _kind(turn.message)
        state = turn.state.state
        expire_stale_assignments(self.db, self.now)

        if state == "awaiting_stop_confirm":
            if kind == "text" and self.catalog.matches("yes", turn.body):
                turn.participant.status = "withdrawn"
                active = get_active_session(self.db, turn.participant.participant_id)
                if active is not None:
                    end_session(self.db, active.id, "abandoned", self.now)
                self._set(turn, "idle", previous_state=None, offered_task_type=None)
                return Reply(parts=[self.r("stop.done")])
            self._set(turn, turn.context.get("previous_state") or "idle", previous_state=None)
            return Reply(parts=[self.r("stop.cancelled")])

        if kind == "text":
            command = self.global_command(turn)
            if command is not None:
                return command

        if state == "awaiting_ready":
            if kind == "text" and self.catalog.matches("yes", turn.body):
                return self.start(turn, turn.context.get("offered_task_type"))
            if kind == "text" and self.catalog.matches("later", turn.body):
                self._set(turn, "idle", offered_task_type=None)
                return Reply(parts=[self.r("ready.later")])
            if kind == "audio":
                return Reply(parts=[self.r("ready.ask")])  # kept as free speech
            return Reply(parts=[self.r("ready.unclear")])

        if state == "awaiting_continue":
            if kind == "text" and self.catalog.matches("yes", turn.body):
                return self.start(turn, turn.context.get("offered_task_type"))
            if kind == "text" and self.catalog.matches("later", turn.body):
                self._set(turn, "idle", offered_task_type=None)
                return Reply(parts=[self.r("session.paused")])
            return Reply(parts=[self.r("ready.unclear")])

        if state == "in_session":
            if kind == "audio":
                return self.audio_in_session(turn)
            open_assignment = find_open_assignment(self.db, turn.participant.participant_id)
            if open_assignment is None:
                stale = get_active_session(self.db, turn.participant.participant_id)
                if stale is not None:
                    end_session(self.db, stale.id, "abandoned", self.now)
                return self.offer(turn)
            if kind == "text":
                if self.catalog.matches("greetings", turn.body):
                    return self.repeat_prompt(turn, open_assignment, self.r("session.open_prompt_reminder"))
                return Reply(parts=[self.r("problems.text_instead_of_audio", **self._values(turn.participant, duration="0"))])
            problem = f"problems.{_MEDIA_PROBLEMS.get(kind, 'other')}_instead_of_audio"
            return Reply(parts=[self.r(problem, **self._values(turn.participant, duration="0"))])

        # idle: anything starts the conversation (audio is kept as free speech).
        return self.offer(turn)


# ==========
# ENTRYPOINT
# ==========
def _lock(db: Session, sender: str) -> None:
    """One turn at a time per sender, until this transaction ends."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"conversation:{sender}"})


def _state_for(db: Session, participant_id: str) -> ConversationState:
    db.execute(
        insert(ConversationState)
        .values(participant_id=participant_id, state="idle", context={})
        .on_conflict_do_nothing(index_elements=[ConversationState.participant_id])
    )
    state = db.get(ConversationState, participant_id, with_for_update=True)
    if state.context is None:
        state.context = {}
    return state


def _unregistered_reply(db: Session, message: InboundMessage, catalog: Catalog, now: datetime) -> Reply | None:
    recent = db.scalar(
        select(func.count(OutboundMessage.id))
        .join(InboundMessage, InboundMessage.id == OutboundMessage.inbound_message_id)
        .where(
            InboundMessage.sender == message.sender,
            OutboundMessage.message_keys.contains([{"key": "unregistered"}]),
            OutboundMessage.created_at > now - UNREGISTERED_REPLY_EVERY,
        )
    )
    if recent:
        return None
    return Reply(parts=[catalog.render("unregistered")])


def respond(message_sid: str, now: datetime | None = None) -> list[int]:
    """Decide and record the reply to one inbound message.

    Returns the IDs of its pending outbound messages in order ([] when the bot
    stays silent). The caller sends only the first: each one sent queues the next.
    """
    now = now or datetime.now(UTC)
    catalog = get_catalog()
    config = get_collection_config()
    with get_sync_sessionmaker()() as db:
        message = db.scalar(select(InboundMessage).where(InboundMessage.message_sid == message_sid))
        if message is None:
            raise LookupError(f"Inbound message {message_sid} not found")
        _lock(db, message.sender)
        existing = db.scalars(
            select(OutboundMessage)
            .where(OutboundMessage.inbound_message_id == message.id)
            .order_by(OutboundMessage.sequence)
        ).all()
        if existing:
            # This turn already happened (a retried job): just resend what's pending.
            return [o.id for o in existing if o.status == "pending"]

        participant = db.scalar(select(Participant).where(Participant.phone_number == message.sender))
        participant_id = participant.participant_id if participant else None
        if participant is None:
            reply = _unregistered_reply(db, message, catalog, now)
        elif participant.status == "withdrawn":
            reply = None
        elif participant.status == "completed":
            reply = Reply(parts=[catalog.render("participant.completed")])
        elif participant.status != "active":
            reply = Reply(parts=[catalog.render("participant.paused")])
        else:
            recording = db.scalar(select(Recording).where(Recording.inbound_message_id == message.id))
            state = _state_for(db, participant_id)
            turn = Turn(db, message, participant, state, recording, now)
            engine = Engine(db, catalog, config, now)
            reply = engine.handle(turn)
            engine._commit_context(turn)
            logger.info(
                "Turn for %s (message id=%s): state %s",
                participant_id,
                message.id,
                state.state,
            )

        if reply is None:
            db.commit()
            logger.info("No reply to message id=%s from %s", message.id, mask_phone(message.sender))
            return []
        rows = emit(db, reply, message.id, participant_id, now)
        db.commit()
        return [row.id for row in rows]
