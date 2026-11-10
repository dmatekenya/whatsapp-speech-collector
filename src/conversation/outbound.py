"""Outbound messages: compose one turn's reply into rows, and send them.

The engine writes outbound_messages rows in the same transaction as the state
change they belong to; the send_outbound job (RQ queue "outbound") sends them
afterwards. A row is sent at most once: the job only acts on rows still
pending, and (inbound_message_id, purpose) is unique, so a retried turn can't
create a second reply.

Template messages (purpose "template:<name>", e.g. the registration welcome in
src/registry/welcome.py) answer nothing: they have no inbound message, go to
the participant's registered number, and are exempt from the 24-hour window
check, which WhatsApp allows for approved templates. Their template, language
and body values are in message_keys[0].
"""

import copy
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from redis import Redis
from rq import Queue, Retry, get_current_job
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.channels.base import TWILIO, ChannelAdapter, MessageSendError
from src.channels.factory import get_channel
from src.config import get_settings
from src.conversation.locks import lock_conversation
from src.conversation.messages import Rendered
from src.conversation.states import IN_SESSION
from src.logging_config import mask_phone
from src.media.urls import MediaUrlError, build_media_url
from src.storage.db import get_sync_sessionmaker
from src.storage.models import (
    Assignment,
    ConversationState,
    InboundMessage,
    OutboundMessage,
    Participant,
    TaskSession,
)
from src.tasks.bank import AssignmentClosedError, end_session, mark_sent
from src.tasks.config import get_collection_config

logger = logging.getLogger(__name__)

OUTBOUND_QUEUE = "outbound"
SEND_JOB = "src.conversation.outbound.send_outbound"
RESTORE_JOB = "src.conversation.outbound.restore_undelivered"
# Statuses of a reply that never reached the participant.
NOT_DELIVERED = ("failed", "skipped_window")
SEND_RETRY = Retry(max=4, interval=[10, 60, 300, 1800])
# WhatsApp's limit for an image caption.
CAPTION_LIMIT = 1024
STATUS_ROUTE = "/whatsapp/status"
# purpose of a template message: TEMPLATE_PREFIX + the template's name.
TEMPLATE_PREFIX = "template:"


def is_template(outbound: OutboundMessage) -> bool:
    return outbound.purpose.startswith(TEMPLATE_PREFIX)


# =========
# COMPOSING
# =========
@dataclass
class PromptPart:
    """A prompt to send in this turn: its text (or caption) and, for images, the prompt ID."""

    assignment: Assignment
    rendered: Rendered
    media_prompt_id: str | None = None


@dataclass
class Reply:
    """One turn's reply: text blocks, then optionally a prompt.

    prompt_prefix (e.g. "✅ Received. ") is joined to the prompt with no gap.
    """

    parts: list[Rendered] = field(default_factory=list)
    prompt: PromptPart | None = None
    prompt_prefix: Rendered | None = None

    def add(self, *rendered: Rendered) -> "Reply":
        self.parts.extend(rendered)
        return self


def _join(parts: list[Rendered]) -> str:
    return "\n\n".join(part.text for part in parts if part.text)


def emit(
    db: Session,
    reply: Reply,
    inbound_message_id: int,
    participant_id: str | None,
    now: datetime | None = None,
    channel: str = TWILIO,
    state_snapshot: dict[str, Any] | None = None,
) -> list[OutboundMessage]:
    """Write the reply as outbound_messages rows (normally one), to go out on
    channel: the channel of the inbound message being answered.

    An image prompt goes in one message with the text as its caption if that
    fits WhatsApp's caption limit; otherwise the text is sent first, then the
    image with just its own caption.
    """
    prompt = reply.prompt
    prompt_text = None
    if prompt is not None:
        # Rendered text is stripped, so the prefix gets its space back here.
        prefix = f"{reply.prompt_prefix.text} " if reply.prompt_prefix else ""
        prompt_text = f"{prefix}{prompt.rendered.text}"
        if prompt.assignment.message_variant is None:
            prompt.assignment.message_variant = prompt.rendered.variant

    keys = [part.record for part in reply.parts]
    if reply.prompt_prefix is not None:
        keys.append(reply.prompt_prefix.record)
    prompt_keys = [prompt.rendered.record] if prompt else []
    leading = _join(reply.parts)

    def row(purpose: str, sequence: int, body: str, records: list, with_prompt: bool) -> OutboundMessage:
        return OutboundMessage(
            participant_id=participant_id,
            inbound_message_id=inbound_message_id,
            purpose=purpose,
            sequence=sequence,
            message_keys=records,
            body=body,
            media_prompt_id=prompt.media_prompt_id if (with_prompt and prompt) else None,
            assignment_id=prompt.assignment.id if (with_prompt and prompt) else None,
            status="pending",
            channel=channel,
            state_snapshot=state_snapshot,
            # The turn's time, so rate limits use the same clock as the engine.
            created_at=now or datetime.now(UTC),
        )

    combined = "\n\n".join(text for text in (leading, prompt_text) if text)
    if prompt is not None and prompt.media_prompt_id is not None and len(combined) > CAPTION_LIMIT:
        rows = [
            row("reply", 1, leading, keys, with_prompt=False),
            row("reply_media", 2, prompt_text or "", prompt_keys, with_prompt=True),
        ]
    else:
        rows = [row("reply", 1, combined, keys + prompt_keys, with_prompt=prompt is not None)]
    db.add_all(rows)
    db.flush()
    return rows


# =======
# SENDING
# =======
@lru_cache
def get_outbound_queue() -> Queue:
    connection = Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)
    return Queue(OUTBOUND_QUEUE, connection=connection)


def enqueue_send(outbound_id: int) -> bool:
    try:
        get_outbound_queue().enqueue(SEND_JOB, outbound_id, retry=SEND_RETRY)
    except Exception as exc:
        logger.error(
            "Could not queue outbound id=%s (%s); scripts/send_pending_outbound.py will pick it up",
            outbound_id,
            type(exc).__name__,
        )
        return False
    return True


def status_callback_url(channel: str = TWILIO) -> str | None:
    """Where Twilio posts delivery statuses. Meta posts them to its webhook instead."""
    if channel != TWILIO:
        return None
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}{STATUS_ROUTE}" if base else None


def _recipient(db: Session, outbound: OutboundMessage) -> str:
    """The number a message goes to: the sender of the message it answers, or for a
    message that answers nothing (a template), the participant's registered number."""
    if outbound.inbound_message_id is None:
        sender = db.scalar(
            select(Participant.phone_number).where(Participant.participant_id == outbound.participant_id)
        )
    else:
        sender = db.scalar(
            select(InboundMessage.sender).where(InboundMessage.id == outbound.inbound_message_id)
        )
    if not sender:
        raise LookupError(f"No recipient for outbound id={outbound.id}")
    return sender


def _last_attempt() -> bool:
    job = get_current_job()
    return job is None or not job.retries_left


def _next_in_turn(db: Session, outbound: OutboundMessage) -> OutboundMessage | None:
    if outbound.inbound_message_id is None:
        return None  # a template message is a turn of its own
    return db.scalar(
        select(OutboundMessage)
        .where(
            OutboundMessage.inbound_message_id == outbound.inbound_message_id,
            OutboundMessage.sequence > outbound.sequence,
            OutboundMessage.status == "pending",
        )
        .order_by(OutboundMessage.sequence)
        .limit(1)
    )


def _hours_since_last_message(db: Session, sender: str, channel: str, now: datetime) -> float | None:
    """Hours since the participant last wrote on this channel (None: never)."""
    latest = db.scalar(
        select(func.max(InboundMessage.received_at)).where(
            InboundMessage.sender == sender, InboundMessage.channel == channel
        )
    )
    return None if latest is None else (now - latest).total_seconds() / 3600


def _log_not_sent(outbound: OutboundMessage, status: str, reason: str) -> None:
    """At WARNING: IDs, channel and the provider's error fields only; never numbers or text."""
    details = outbound.error_details or {}
    logger.warning(
        "Outbound id=%s (%s, %s) for %s not sent [%s]: %s%s",
        outbound.id,
        outbound.purpose,
        outbound.channel,
        outbound.participant_id or "unregistered sender",
        status,
        reason,
        "".join(f" {key}={details[key]}" for key in ("code", "subcode", "title", "fbtrace_id") if key in details),
    )


def _not_delivered(db: Session, outbound: OutboundMessage, status: str, now: datetime) -> None:
    """Mark a reply not delivered (failed or skipped_window), with the rest of
    its turn, and put the conversation back as it was before that turn."""
    outbound.status = status
    outbound.status_updated_at = now
    if outbound.inbound_message_id is None:
        return  # a template message: no rest of a turn, no conversation to restore
    # The rest of the reply goes out in order or not at all.
    for later in db.scalars(
        select(OutboundMessage).where(
            OutboundMessage.inbound_message_id == outbound.inbound_message_id,
            OutboundMessage.sequence > outbound.sequence,
            OutboundMessage.status == "pending",
        )
    ):
        later.status = status
        later.status_updated_at = now
        later.error = "Not sent: an earlier message of this reply wasn't sent"
    restore_conversation(db, outbound)


def send_outbound(
    outbound_id: int, channel: ChannelAdapter | None = None, now: datetime | None = None
) -> str | None:
    """Send one pending outbound message; returns the provider's message ID.

    It goes out on the row's channel (that of the inbound message it answers),
    never a global setting.

    Nothing is sent if the participant's last message on that channel is
    older than reply_window_hours (WhatsApp's 24-hour window): the row becomes
    skipped_window, as it does when the provider refuses it for that reason
    (Meta 131047, Twilio 63016), and it is never retried. Other transient
    failures raise so RQ retries; a permanent failure (or the last retry)
    marks the row failed. Either way the provider's error is stored, and the
    conversation goes back to its state before this turn, so the
    participant's next message repeats the step.

    Template messages skip the window check (WhatsApp allows them outside the
    window) and go out with the adapter's send_template.

    The next message in the same turn is queued after this one is sent, so a
    long text always arrives before its image. Called with a channel
    (simulator, tests), the rest of the turn is sent straight away.
    """
    direct = channel is not None
    now = now or datetime.now(UTC)
    with get_sync_sessionmaker()() as db:
        outbound = db.get(OutboundMessage, outbound_id, with_for_update=True)
        if outbound is None or outbound.status != "pending":
            return None
        to = _recipient(db, outbound)

        template = is_template(outbound)
        window = get_collection_config().reply_window_hours
        age = None if template else _hours_since_last_message(db, to, outbound.channel, now)
        if not template and (age is None or age > window):
            outbound.error = (
                f"Not sent: the participant's last message on this channel was "
                f"{'never' if age is None else f'{age:.1f} h ago'} (reply window {window:g} h)"
            )
            _not_delivered(db, outbound, "skipped_window", now)
            db.commit()
            _log_not_sent(outbound, "skipped_window", outbound.error)
            return None

        adapter = channel or get_channel(outbound.channel)
        callback = status_callback_url(outbound.channel)
        try:
            if template:
                sid = _send_template(adapter, to, outbound)
            elif outbound.media_prompt_id:
                url = build_media_url(outbound.media_prompt_id)
                sid = adapter.send_media(to, url, caption=outbound.body or None, status_callback=callback)
            else:
                sid = adapter.send_text(to, outbound.body, status_callback=callback)
        except (MessageSendError, MediaUrlError) as exc:
            outbound.error = str(exc)
            outbound.error_code = getattr(exc, "code", None)
            outbound.error_details = getattr(exc, "details", None)
            if isinstance(exc, MessageSendError) and exc.outside_window:
                _not_delivered(db, outbound, "skipped_window", now)
                db.commit()
                _log_not_sent(outbound, "skipped_window", "outside the 24-hour window (provider refused)")
                return None
            if isinstance(exc, MediaUrlError) or exc.permanent or _last_attempt():
                _not_delivered(db, outbound, "failed", now)
                db.commit()
                _log_not_sent(outbound, "failed", str(exc))
                return None
            db.commit()
            raise
        except Exception as exc:
            if _last_attempt():
                outbound.error = type(exc).__name__
                _not_delivered(db, outbound, "failed", now)
                db.commit()
                _log_not_sent(outbound, "failed", type(exc).__name__)
                return None
            raise

        outbound.provider_message_id = sid
        outbound.status = "sent"
        outbound.sent_at = now
        outbound.error = None
        outbound.error_code = None
        outbound.error_details = None
        if outbound.assignment_id is not None:
            try:
                # The expiry clock and swipe-reply binding start from here.
                mark_sent(db, outbound.assignment_id, sid, now)
            except AssignmentClosedError:
                pass  # e.g. it expired before the send went out
        db.commit()
        logger.info(
            "Sent outbound id=%s (%s) via %s to %s", outbound.id, outbound.purpose, outbound.channel, mask_phone(to)
        )
        following = _next_in_turn(db, outbound)
        following_id = following.id if following else None

    if following_id is not None:
        if direct:
            send_outbound(following_id, channel, now)
        else:
            enqueue_send(following_id)
    return sid


def _send_template(adapter: ChannelAdapter, to: str, outbound: OutboundMessage) -> str:
    """Send a template row: name, language and body values from message_keys[0]."""
    send = getattr(adapter, "send_template", None)
    if send is None:
        raise MessageSendError(f"Templates can't be sent on the {outbound.channel} channel", permanent=True)
    spec = (outbound.message_keys or [{}])[0]
    name = spec.get("template") or outbound.purpose.removeprefix(TEMPLATE_PREFIX)
    return send(to, name, spec.get("language") or "", list(spec.get("params") or []))


# ================================
# ROLLING BACK AN UNDELIVERED TURN
# ================================
def restore_conversation(db: Session, outbound: OutboundMessage) -> bool:
    """Put the conversation back to its state before the turn that wrote this
    reply, so the participant's next message repeats the step.

    Only if nothing has changed it since (a later turn wins). Recordings,
    answers and withdrawals are never undone. A prompt of this turn that never
    went out stays open if the conversation goes back into the session (the
    next message re-sends it); otherwise it expires, and a session started by
    this turn is abandoned. Returns True if the state was restored.
    """
    snapshot = outbound.state_snapshot
    if not snapshot or outbound.participant_id is None:
        return False
    lock_conversation(db, _recipient(db, outbound))
    state = db.get(ConversationState, outbound.participant_id, with_for_update=True)
    if state is None or {"state": state.state, "context": state.context or {}} != snapshot["after"]:
        logger.info(
            "Outbound id=%s not delivered; the conversation for %s has moved on, so it is kept",
            outbound.id,
            outbound.participant_id,
        )
        return False

    before = snapshot["before"]
    state.state = before["state"]
    state.context = copy.deepcopy(before["context"])
    flag_modified(state, "context")
    if before["state"] != IN_SESSION:
        _close_undelivered_prompts(db, outbound)
    db.flush()
    logger.info(
        "Outbound id=%s not delivered; the conversation for %s is back to %s",
        outbound.id,
        outbound.participant_id,
        before["state"],
    )
    return True


def _close_undelivered_prompts(db: Session, outbound: OutboundMessage) -> None:
    rows = db.scalars(
        select(OutboundMessage).where(
            OutboundMessage.inbound_message_id == outbound.inbound_message_id,
            OutboundMessage.assignment_id.is_not(None),
        )
    )
    for row in rows:
        assignment = db.get(Assignment, row.assignment_id, with_for_update=True)
        if assignment is None or assignment.status != "sent" or assignment.sent_at is not None:
            continue
        assignment.status = "expired"
        task_session = db.get(TaskSession, assignment.session_id)
        # Started by this turn (same clock), and the conversation is no longer in it.
        if task_session is not None and task_session.status == "active" and task_session.started_at >= outbound.created_at:
            end_session(db, task_session.id, "abandoned", outbound.created_at)


def enqueue_restore(outbound_id: int) -> None:
    """Queue restore_undelivered for a reply whose delivery failed later (status webhook)."""
    get_outbound_queue().enqueue(RESTORE_JOB, outbound_id, retry=SEND_RETRY)


def restore_undelivered(outbound_id: int) -> None:
    """Job: a reply was accepted but its delivery failed (Meta statuses, Twilio
    callbacks). Put the conversation back, as for a failed send, unless part
    of that reply already reached the participant or a later turn moved on."""
    with get_sync_sessionmaker()() as db:
        outbound = db.get(OutboundMessage, outbound_id, with_for_update=True)
        if outbound is None or outbound.status not in NOT_DELIVERED:
            return
        reached = db.scalar(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.inbound_message_id == outbound.inbound_message_id,
                OutboundMessage.status.in_(("delivered", "read")),
            )
        )
        if not reached:
            restore_conversation(db, outbound)
        db.commit()
