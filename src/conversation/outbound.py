"""Outbound messages: compose one turn's reply into rows, and send them.

The engine writes outbound_messages rows in the same transaction as the state
change they belong to; the send_outbound job (RQ queue "outbound") sends them
afterwards. A row is sent at most once: the job only acts on rows still
pending, and (inbound_message_id, purpose) is unique, so a retried turn can't
create a second reply.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache

from redis import Redis
from rq import Queue, Retry, get_current_job
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.channels.base import ChannelAdapter, MessageSendError
from src.channels.factory import get_channel
from src.config import get_settings
from src.conversation.messages import Rendered
from src.logging_config import mask_phone
from src.media.urls import MediaUrlError, build_media_url
from src.storage.db import get_sync_sessionmaker
from src.storage.models import Assignment, InboundMessage, OutboundMessage
from src.tasks.bank import AssignmentClosedError, mark_sent

logger = logging.getLogger(__name__)

OUTBOUND_QUEUE = "outbound"
SEND_JOB = "src.conversation.outbound.send_outbound"
SEND_RETRY = Retry(max=4, interval=[10, 60, 300, 1800])
# WhatsApp's limit for an image caption.
CAPTION_LIMIT = 1024
STATUS_ROUTE = "/whatsapp/status"


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
) -> list[OutboundMessage]:
    """Write the reply as outbound_messages rows (normally one).

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


def status_callback_url() -> str | None:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}{STATUS_ROUTE}" if base else None


def _recipient(db: Session, outbound: OutboundMessage) -> str:
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


def send_outbound(
    outbound_id: int, channel: ChannelAdapter | None = None, now: datetime | None = None
) -> str | None:
    """Send one pending outbound message; returns the channel's message SID.

    Transient failures raise so RQ retries; a permanent failure (or the last
    retry) marks the row failed and is logged. The conversation state is
    never rolled back. The next message in the same turn is queued after this
    one is sent, so a long text always arrives before its image. Called with
    a channel (simulator, tests), the rest of the turn is sent straight away.
    """
    direct = channel is not None
    channel = channel or get_channel()
    with get_sync_sessionmaker()() as db:
        outbound = db.get(OutboundMessage, outbound_id, with_for_update=True)
        if outbound is None or outbound.status != "pending":
            return None
        to = _recipient(db, outbound)
        try:
            if outbound.media_prompt_id:
                url = build_media_url(outbound.media_prompt_id)
                sid = channel.send_media(
                    to, url, caption=outbound.body or None, status_callback=status_callback_url()
                )
            else:
                sid = channel.send_text(to, outbound.body, status_callback=status_callback_url())
        except (MessageSendError, MediaUrlError) as exc:
            permanent = isinstance(exc, MediaUrlError) or exc.permanent
            outbound.error = str(exc)
            outbound.error_code = getattr(exc, "code", None)
            if permanent or _last_attempt():
                outbound.status = "failed"
                db.commit()
                logger.error("Outbound id=%s to %s failed: %s", outbound.id, mask_phone(to), exc)
                return None
            db.commit()
            raise
        except Exception as exc:
            if _last_attempt():
                outbound.status = "failed"
                outbound.error = type(exc).__name__
                db.commit()
                logger.error("Outbound id=%s to %s failed: %s", outbound.id, mask_phone(to), type(exc).__name__)
                return None
            raise

        now = now or datetime.now(UTC)
        outbound.twilio_sid = sid
        outbound.status = "sent"
        outbound.sent_at = now
        outbound.error = None
        if outbound.assignment_id is not None:
            try:
                # The expiry clock and swipe-reply binding start from here.
                mark_sent(db, outbound.assignment_id, sid, now)
            except AssignmentClosedError:
                pass  # e.g. it expired before the send went out
        db.commit()
        logger.info("Sent outbound id=%s (%s) to %s", outbound.id, outbound.purpose, mask_phone(to))
        following = _next_in_turn(db, outbound)
        following_id = following.id if following else None

    if following_id is not None:
        if direct:
            send_outbound(following_id, channel, now)
        else:
            enqueue_send(following_id)
    return sid
