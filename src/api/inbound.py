"""What both webhooks do with what they receive.

store_and_enqueue: save an inbound message (deduplicating on the provider's
message ID, since providers retry webhooks), then queue it for the worker.
apply_status: move an outbound message's delivery status forward.
"""

import logging
from datetime import UTC, datetime

from rq import Queue
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.channels.base import InboundMessage as ChannelMessage
from src.channels.base import WINDOW_ERROR_CODES, StatusUpdate
from src.conversation.outbound import NOT_DELIVERED, enqueue_restore
from src.logging_config import mask_message_id, mask_phone
from src.storage.models import InboundMessage, OutboundMessage
from src.storage.queue import enqueue_inbound

logger = logging.getLogger(__name__)

# Delivery statuses only move forward; "failed" can replace any of them but "read".
_STATUS_RANK = {"pending": 0, "sent": 1, "delivered": 2, "read": 3}


# =======
# INBOUND
# =======
async def store_and_enqueue(
    session: AsyncSession, queue: Queue, channel: str, message: ChannelMessage
) -> bool:
    """Store one inbound message and queue it. False if it was a duplicate.

    Logs only the (masked) message ID, the masked sender and the type, never the text.
    """
    label = mask_message_id(message.message_id)
    logger.info(
        "Inbound %s message id=%s from=%s type=%s media_content_type=%s",
        channel,
        label,
        mask_phone(message.sender),
        message.type,
        message.media_content_type,
    )
    row_id = await session.scalar(
        insert(InboundMessage)
        .values(
            message_sid=message.message_id,
            channel=channel,
            sender=message.sender,
            message_type=message.type.value,
            raw_payload=message.raw,
        )
        .on_conflict_do_nothing(index_elements=[InboundMessage.message_sid])
        .returning(InboundMessage.id)
    )
    await session.commit()
    if row_id is None:
        logger.info("Inbound message id=%s is a duplicate, not enqueuing", label)
        return False

    # The message is safely in Postgres; if Redis is down it stays 'received'
    # and scripts/requeue_pending.py picks it up later.
    try:
        await run_in_threadpool(enqueue_inbound, queue, message.message_id)
    except Exception:
        logger.exception("Failed to enqueue inbound message id=%s", label)
        return True

    # Only move 'received' -> 'queued', so a fast worker's 'processed' isn't overwritten.
    await session.execute(
        update(InboundMessage)
        .where(InboundMessage.id == row_id, InboundMessage.status == "received")
        .values(status="queued")
    )
    await session.commit()
    return True


# ===============
# DELIVERY STATUS
# ===============
async def apply_status(session: AsyncSession, channel: str, status: StatusUpdate) -> bool:
    """Apply one delivery status. False if the message isn't ours or the status isn't tracked.

    A failure because the participant is outside WhatsApp's 24-hour window
    (Meta 131047, Twilio 63016) is stored as skipped_window. When a reply
    turns out not to be delivered, the conversation is rolled back to before
    its turn (the restore_undelivered job), as for a failed send.
    """
    outbound = await session.scalar(
        select(OutboundMessage)
        .where(
            OutboundMessage.channel == channel,
            OutboundMessage.provider_message_id == status.message_id,
        )
        .with_for_update()
    )
    if outbound is None or status.status is None:
        return False

    newly_undelivered = False
    if status.status == "failed":
        if outbound.status not in ("read", *NOT_DELIVERED):
            outside_window = status.error_code in WINDOW_ERROR_CODES
            outbound.status = "skipped_window" if outside_window else "failed"
            outbound.error_code = status.error_code
            outbound.error = status.error
            outbound.error_details = status.details
            newly_undelivered = True
            details = status.details or {}
            logger.warning(
                "Outbound id=%s (%s) for %s not delivered [%s]: error %s%s",
                outbound.id,
                channel,
                outbound.participant_id or "unregistered sender",
                outbound.status,
                status.error_code or "?",
                f" title={details['title']}" if "title" in details else "",
            )
    elif outbound.status not in NOT_DELIVERED and _STATUS_RANK.get(status.status, 0) > _STATUS_RANK.get(
        outbound.status, 0
    ):
        outbound.status = status.status
    outbound.status_updated_at = datetime.now(UTC)
    await session.commit()

    if newly_undelivered:
        try:
            await run_in_threadpool(enqueue_restore, outbound.id)
        except Exception as exc:
            logger.error(
                "Could not queue the conversation rollback for outbound id=%s (%s)", outbound.id, type(exc).__name__
            )
    return True
