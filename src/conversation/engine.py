"""The conversation engine: what the bot says in reply to each inbound message.

respond(message_sid) runs inside process_inbound, after the audio has been
stored. Per turn, in one transaction: take an advisory lock on the sender
(so one participant's messages are handled one at a time), read the state,
decide, write the new state and the reply's outbound_messages rows, commit.
The send_outbound job then sends them. Re-running a turn whose reply exists
just returns that reply's pending rows, so a retried job never replies twice.

States: idle | awaiting_ready | in_session | awaiting_continue | awaiting_stop_confirm.

After a gap longer than fresh_start_after_hours since the participant's previous
message, a non-idle conversation starts afresh: the open session is paused,
its unanswered prompts expire, and the message gets the welcome-back offer.
Each reply row keeps the state before and after its turn, so the send job can
put the conversation back if the reply is never delivered (see outbound.py).
"""

import copy
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.channels.base import InboundMessage as ChannelMessage
from src.channels.factory import get_channel
from src.config import get_settings
from src.conversation.locks import lock_conversation
from src.conversation.states import (  # noqa: F401 - STATES is re-exported
    AWAITING_CONTINUE,
    AWAITING_READY,
    AWAITING_STOP_CONFIRM,
    IDLE,
    IN_SESSION,
    INITIAL_STATE,
    STATES,
    initial_context,
)
from src.conversation.messages import Catalog, Rendered, get_catalog
from src.conversation.outbound import PromptPart, Reply, emit
from src.logging_config import mask_phone
from src.storage.audio import apply_audio_origin, apply_length_flag
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
from src.tasks.config import CollectionConfig, get_collection_config

logger = logging.getLogger(__name__)

UNREGISTERED_REPLY_EVERY = timedelta(hours=24)
# Shorter than this, a voice note is treated as empty.
EMPTY_AUDIO_SECONDS = 0.5
# Catalog problem -> recordings.qc_reason for clips rejected as failed attempts.
FAILED_ATTEMPT_REASONS = {
    "empty_audio": "empty",
    "too_short": "too_short",
    "too_long": "too_long",
    "no_speech": "no_speech",
}
_MEDIA_PROBLEMS = {"image": "image", "video": "video", "document": "document", "other": "other"}


# =======
# HELPERS
# =======
def _kind(message: InboundMessage, inbound: ChannelMessage) -> str:
    """text | audio | image | video | document | other."""
    if message.message_type in ("text", "audio"):
        return message.message_type
    content_type = inbound.media_content_type or ""
    for prefix, kind in (("image/", "image"), ("video/", "video"), ("application/", "document"), ("text/", "document")):
        if content_type.startswith(prefix):
            return kind
    return "other"


def _first_name(participant: Participant) -> str:
    return (participant.name or "").split()[0] if participant.name else "there"


def _minutes(value: float) -> str:
    return f"{value:.1f}"


def length_text(seconds: float) -> str:
    """A clip limit as participants read it: "45 seconds", "1 minute", "2 minutes"."""
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{seconds:g} seconds"


@dataclass
class Turn:
    db: Session
    message: InboundMessage
    participant: Participant
    state: ConversationState
    recording: Recording | None
    now: datetime
    # When the participant's previous message (any channel) arrived; None if this is their first.
    previous_inbound_at: datetime | None = None

    @cached_property
    def inbound(self) -> ChannelMessage:
        """The stored message, parsed by the adapter of the channel it arrived on."""
        return get_channel(self.message.channel).parse_inbound(self.message.raw_payload or {})

    @property
    def body(self) -> str:
        return self.inbound.text or ""

    @property
    def replied_to(self) -> str | None:
        """The provider message ID of the message this swipe-replies to."""
        return self.inbound.replied_to_message_id

    @property
    def channel(self) -> str:
        return self.message.channel

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
            # Always the sum of the per-type targets.
            "total_target": _minutes(self.config.total(targets)),
            "session_size": self.config.session_size,
            "min_seconds": f"{self.config.clip_limits.min_seconds:g}",
            **self._length_values(None),
            "support_contact": get_settings().support_contact,
        }
        for task_type in types:
            values[f"{task_type}_done"] = _minutes(done.get(task_type, 0.0))
            values[f"{task_type}_target"] = _minutes(targets.get(task_type, 0.0))
        values.update(extra)
        return values

    def _length_values(self, task_type: str | None, origin: str | None = None) -> dict[str, str]:
        """max_length: the limit for this task type and origin; max_length_older: for an
        older voice note or audio file (forwarded / uploaded) in this task type."""
        limits = self.config.clip_limits
        return {
            "max_length": length_text(limits.max_seconds(task_type, origin)),
            "max_length_older": length_text(limits.max_seconds(task_type, "forwarded")),
        }

    def task_name(self, task_type: str) -> str:
        """Lowercase, for use mid-sentence ("Next up: *reading sentences*")."""
        return self.catalog.text(f"task_names.{task_type}")

    def task_label(self, task_type: str) -> str:
        """Capitalised, for lists ("Reading sentences")."""
        return self.catalog.text(f"task_labels.{task_type}")

    def _progress(self, participant: Participant) -> Rendered:
        """The summary line, then one progress.line per task type (types with
        nothing to record and nothing recorded are left out)."""
        done = minutes_recorded(self.db, participant.participant_id)
        if sum(done.values()) <= 0:
            return self.r("progress.none_yet")
        values = self._values(participant)
        targets = self.config.targets_for(participant.attributes)
        header = self.r("progress.summary", **values)
        lines = [
            self.r(
                "progress.line",
                label=self.task_label(t),
                done=values[f"{t}_done"],
                target=values[f"{t}_target"],
            ).text
            for t in self.config.task_types
            if targets.get(t, 0.0) > 0 or done.get(t, 0.0) > 0
        ]
        return Rendered(header.key, header.variant, "\n".join([header.text, *lines]))

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
                lengths = self._length_values(task_type) if key == task_type else {}
                parts.append(self.r(f"instructions.{key}", **self._values(turn.participant, **lengths)))
                seen[key] = True
        if parts:
            parts.append(self.r("instructions.reminder"))
        turn.context["instructions_seen"] = seen
        return parts

    def _prompt(self, assignment: Assignment) -> PromptPart:
        task_session = self.db.get(TaskSession, assignment.session_id)
        size = task_session.size if task_session else self.config.session_size
        if not self.config.is_prompted(assignment.prompt_type):
            # free_speech: an invitation to talk (variants suggest topics).
            rendered = self.r(f"prompt.{assignment.prompt_type}", n=assignment.position, session_size=size)
            return PromptPart(assignment, rendered)
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

    # ===========
    # FRESH START
    # ===========

    def _after_a_gap(self, turn: Turn) -> bool:
        if turn.previous_inbound_at is None:
            return False
        received = turn.message.received_at or self.now
        return received - turn.previous_inbound_at > timedelta(hours=self.config.fresh_start_hours)

    def _swiped_onto_open_prompt(self, turn: Turn, kind: str) -> bool:
        """A voice note swipe-replying to a prompt that hasn't expired: answered as usual."""
        if kind != "audio" or not turn.replied_to:
            return False
        assignment = find_open_assignment(self.db, turn.participant.participant_id, turn.replied_to, turn.channel)
        return (
            assignment is not None
            and assignment.outbound_message_sid == turn.replied_to
            and assignment.status in ANSWERABLE_STATUSES
            and assignment.status != "expired"
            and assignment.expires_at is not None
            and assignment.expires_at > self.now
        )

    def fresh_start(self, turn: Turn) -> None:
        """Pause the open session, expire its unanswered prompts, back to idle."""
        participant_id = turn.participant.participant_id
        active = get_active_session(self.db, participant_id)
        if active is not None:
            self.db.execute(
                update(Assignment)
                .where(Assignment.session_id == active.id, Assignment.status == "sent")
                .values(status="expired")
                .execution_options(synchronize_session="fetch")
            )
            end_session(self.db, active.id, "paused", self.now)
        logger.info(
            "Fresh start for %s after %.1f h without messages (was %s)",
            participant_id,
            ((turn.message.received_at or self.now) - turn.previous_inbound_at).total_seconds() / 3600,
            turn.state.state,
        )
        self._set(turn, IDLE, offered_task_type=None, previous_state=None, session_id=None)

    def _undelivered_prompt(self, participant_id: str) -> Assignment | None:
        """A prompt in the active session that was never delivered (its send failed)."""
        task_session = get_active_session(self.db, participant_id)
        if task_session is None:
            return None
        return self.db.scalar(
            select(Assignment)
            .where(
                Assignment.session_id == task_session.id,
                Assignment.status == "sent",
                Assignment.sent_at.is_(None),
            )
            .order_by(Assignment.position.desc())
            .limit(1)
        )

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
            self._set(turn, IDLE, offered_task_type=None)
            return reply
        reply.add(self._progress(participant), self.r(f"today.{task_type}"))
        reply.add(*self._instructions(turn, task_type))
        reply.add(self.r("ready.ask"))
        self._set(turn, AWAITING_READY, offered_task_type=task_type)
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
            self._set(turn, IDLE, offered_task_type=None)
            return reply
        reply.add(*self._instructions(turn, task_type))
        reply.prompt = self._prompt(assignment)
        self._set(turn, IN_SESSION, offered_task_type=None, session_id=task_session.id)
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
                self._set(turn, IN_SESSION)
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
            self._set(turn, AWAITING_CONTINUE, offered_task_type=next_type)
        else:
            reply.add(self._nothing_to_offer(turn.participant))
            self._set(turn, IDLE, offered_task_type=None)
        return reply

    def repeat_prompt(self, turn: Turn, assignment: Assignment, *parts: Rendered) -> Reply:
        reply = Reply(parts=list(parts))
        reply.prompt = self._prompt(assignment)
        return reply

    # ==================
    # AUDIO IN A SESSION
    # ==================

    def _apply_task_rules(self, turn: Turn, assignment: Assignment) -> bool:
        """Apply config audio_origin and the length flag for the task type this clip
        answers (the worker could only apply the defaults). True if the origin rule
        rejects it (e.g. forwarded or uploaded, except in free_speech)."""
        recording = turn.recording
        if recording is None or recording.status != "stored":
            return False
        apply_audio_origin(recording, recording.audio_origin, assignment.prompt_type, self.config)
        apply_length_flag(recording, assignment.prompt_type, self.config)
        return self.config.audio_policy(recording.audio_origin, assignment.prompt_type) == "reject"

    def _problem(self, recording: Recording | None, task_type: str) -> str | None:
        duration = recording.duration_s if recording is not None else None
        if recording is None or recording.status != "stored" or not duration or duration < EMPTY_AUDIO_SECONDS:
            return "empty_audio"
        if recording.duplicate_of_recording_id is not None:
            return "duplicate"
        limits = self.config.clip_limits
        if duration < limits.min_seconds:
            return "too_short"
        if duration > limits.max_seconds(task_type, recording.audio_origin):
            return "too_long"
        if self.config.clip_checks.problem(recording.active_seconds, duration) == "no_speech":
            return "no_speech"
        return None

    def _flag_low_speech(self, recording: Recording) -> None:
        """Accepted, but mostly silence: marked for review. A clip already flagged
        (e.g. speaker_check_pending for a forwarded or uploaded clip in free speech) keeps its reason."""
        if (
            self.config.clip_checks.problem(recording.active_seconds, recording.duration_s) == "low_speech"
            and recording.qc_status == "pending"
        ):
            recording.qc_status, recording.qc_reason = "needs_review", "low_speech"

    def audio_in_session(self, turn: Turn) -> Reply:
        participant_id = turn.participant.participant_id
        assignment = find_open_assignment(self.db, participant_id, turn.replied_to, turn.channel)
        if assignment is not None and assignment.status not in ANSWERABLE_STATUSES:
            assignment = find_open_assignment(self.db, participant_id)
        if assignment is None:
            # Nothing open (e.g. it expired): close the session, start afresh.
            stale = get_active_session(self.db, participant_id)
            if stale is not None:
                end_session(self.db, stale.id, "abandoned", self.now)
            return self.offer(turn)

        if self._apply_task_rules(turn, assignment):
            # e.g. forwarded or uploaded: never used. The prompt stays open, and it
            # doesn't use up a retake. The reply says which ("forwarded" would be wrong for a file).
            record_failed_attempt(self.db, assignment.id, turn.recording.id, "not_recorded")
            key = "problems.uploaded_audio_file" if turn.recording.audio_origin == "uploaded" else "problems.not_recorded"
            return self.repeat_prompt(turn, assignment, self.r(key))

        problem = self._problem(turn.recording, assignment.prompt_type)
        values = self._values(
            turn.participant,
            duration=f"{(turn.recording.duration_s or 0) if turn.recording else 0:.0f}",
            **self._length_values(assignment.prompt_type, turn.recording.audio_origin if turn.recording else None),
        )
        if problem == "duplicate":
            return self.repeat_prompt(turn, assignment, self.r("problems.duplicate", **values))
        if problem is not None:
            # A failed attempt at this prompt: linked to it, rejected, never
            # an unassigned recording and never counted.
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

        self._flag_low_speech(turn.recording)
        outcome = handle_response(self.db, assignment.id, turn.recording.id, self.now, self.config)
        if outcome.session_ended:
            finished = self.task_name(assignment.prompt_type)
            if outcome.next_task_type is not None:
                key_values = self._values(
                    turn.participant,
                    task_name=finished,
                    next_task_name=self.task_name(outcome.next_task_type),
                )
                self._set(turn, AWAITING_CONTINUE, offered_task_type=outcome.next_task_type)
                return Reply(parts=[self.r("session.type_target_reached", **key_values)])
            self._set(turn, IDLE, offered_task_type=None)
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
            self._set(turn, AWAITING_STOP_CONFIRM, previous_state=turn.state.state)
            return Reply(parts=[self.r("stop.confirm")])
        return None

    def handle(self, turn: Turn) -> Reply | None:
        kind = _kind(turn.message, turn.inbound)
        expire_stale_assignments(self.db, self.now)
        if (
            turn.state.state != IDLE
            and self._after_a_gap(turn)
            and not self._swiped_onto_open_prompt(turn, kind)
        ):
            self.fresh_start(turn)
        state = turn.state.state

        if state == AWAITING_STOP_CONFIRM:
            if kind == "text" and self.catalog.matches("yes", turn.body):
                turn.participant.status = "withdrawn"
                active = get_active_session(self.db, turn.participant.participant_id)
                if active is not None:
                    end_session(self.db, active.id, "abandoned", self.now)
                self._set(turn, IDLE, previous_state=None, offered_task_type=None)
                return Reply(parts=[self.r("stop.done")])
            self._set(turn, turn.context.get("previous_state") or IDLE, previous_state=None)
            return Reply(parts=[self.r("stop.cancelled")])

        if kind == "text":
            command = self.global_command(turn)
            if command is not None:
                return command

        if state == AWAITING_READY:
            if kind == "text" and self.catalog.matches("yes", turn.body):
                return self.start(turn, turn.context.get("offered_task_type"))
            if kind == "text" and self.catalog.matches("later", turn.body):
                self._set(turn, IDLE, offered_task_type=None)
                return Reply(parts=[self.r("ready.later")])
            if kind == "audio":
                return Reply(parts=[self.r("ready.ask")])  # kept as an unassigned recording
            return Reply(parts=[self.r("ready.unclear")])

        if state == AWAITING_CONTINUE:
            if kind == "text" and self.catalog.matches("yes", turn.body):
                return self.start(turn, turn.context.get("offered_task_type"))
            if kind == "text" and self.catalog.matches("later", turn.body):
                self._set(turn, IDLE, offered_task_type=None)
                return Reply(parts=[self.r("session.paused")])
            return Reply(parts=[self.r("ready.unclear")])

        if state == IN_SESSION:
            # The last prompt never reached them (its send failed): send it again.
            undelivered = self._undelivered_prompt(turn.participant.participant_id)
            if undelivered is not None and find_open_assignment(self.db, turn.participant.participant_id) is None:
                return self.repeat_prompt(turn, undelivered)
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

        # idle: anything starts the conversation (audio is kept as an unassigned
        # recording; the worker has applied the global audio_origin rule to it).
        return self.offer(turn)


# ==========
# ENTRYPOINT
# ==========
def _state_for(db: Session, participant_id: str) -> ConversationState:
    db.execute(
        insert(ConversationState)
        .values(participant_id=participant_id, state=INITIAL_STATE, context=initial_context())
        .on_conflict_do_nothing(index_elements=[ConversationState.participant_id])
    )
    state = db.get(ConversationState, participant_id, with_for_update=True)
    if state.context is None:
        state.context = {}
    return state


def _snapshot(state: ConversationState) -> dict[str, Any]:
    return {"state": state.state, "context": copy.deepcopy(state.context or {})}


def _previous_inbound_at(db: Session, message: InboundMessage) -> datetime | None:
    """When the sender's previous message arrived, on any channel."""
    return db.scalar(
        select(func.max(InboundMessage.received_at)).where(
            InboundMessage.sender == message.sender,
            InboundMessage.id != message.id,
            InboundMessage.received_at <= message.received_at,
        )
    )


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
    state_snapshot: dict[str, Any] | None = None
    with get_sync_sessionmaker()() as db:
        message = db.scalar(select(InboundMessage).where(InboundMessage.message_sid == message_sid))
        if message is None:
            raise LookupError(f"Inbound message {message_sid} not found")
        lock_conversation(db, message.sender)
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
            before = _snapshot(state)
            turn = Turn(db, message, participant, state, recording, now, _previous_inbound_at(db, message))
            engine = Engine(db, catalog, config, now)
            reply = engine.handle(turn)
            engine._commit_context(turn)
            state_snapshot = {"before": before, "after": _snapshot(state)}
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
        # Replies go out on the channel the message arrived on.
        rows = emit(db, reply, message.id, participant_id, now, channel=message.channel, state_snapshot=state_snapshot)
        db.commit()
        return [row.id for row in rows]
