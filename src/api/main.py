import logging
import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from rq import Queue
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.api.media import router as media_router
from src.channels.factory import get_channel
from src.config import get_settings
from src.logging_config import mask_phone, setup_logging
from src.storage.db import get_async_session
from src.storage.models import InboundMessage, OutboundMessage
from src.storage.queue import enqueue_inbound, get_inbound_queue

setup_logging()
logger = logging.getLogger(__name__)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

app = FastAPI(title="whatsapp-speech-collector")
app.include_router(media_router)


# =======
# HELPERS
# =======
def empty_twiml() -> Response:
    return Response(content=EMPTY_TWIML, media_type="application/xml")


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: str(value) for key, value in form.items()}


def _signature_ok(request: Request, params: dict[str, str]) -> bool:
    """Twilio's X-Twilio-Signature check (skipped if TWILIO_VALIDATE_SIGNATURE is off)."""
    settings = get_settings()
    if not settings.twilio_validate_signature:
        return True
    # Behind ngrok/nginx the URL FastAPI sees differs from the one Twilio
    # signed, so rebuild it from the public base URL.
    url = settings.public_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += f"?{request.url.query}"
    signature = request.headers.get("X-Twilio-Signature", "")
    return get_channel().validate_request(url, params, signature)


# Delivery statuses only move forward; "failed" can replace any of them but "read".
_STATUS_RANK = {"pending": 0, "sent": 1, "delivered": 2, "read": 3}
_TWILIO_STATUSES = {
    "accepted": "sent",
    "queued": "sent",
    "sending": "sent",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
}
_URLS = re.compile(r"https?://\S+")


# ======
# ROUTES
# ======
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    queue: Annotated[Queue, Depends(get_inbound_queue)],
) -> Response:
    params = await _form(request)
    channel = get_channel()
    if not _signature_ok(request, params):
        logger.warning("Rejected webhook with invalid Twilio signature")
        return Response(status_code=403)

    message = channel.parse_inbound(params)
    logger.info(
        "Inbound message id=%s from=%s type=%s media_content_type=%s",
        message.message_id,
        mask_phone(message.sender),
        message.type,
        message.media_content_type,
    )

    # Store first, deduplicating on MessageSid: Twilio retries webhooks.
    row_id = await session.scalar(
        insert(InboundMessage)
        .values(
            message_sid=message.message_id,
            sender=message.sender,
            message_type=message.type.value,
            raw_payload=message.raw,
        )
        .on_conflict_do_nothing(index_elements=[InboundMessage.message_sid])
        .returning(InboundMessage.id)
    )
    await session.commit()
    if row_id is None:
        logger.info("Inbound message id=%s is a duplicate, not enqueuing", message.message_id)
        return empty_twiml()

    # The message is safely in Postgres; if Redis is down it stays 'received'
    # and scripts/requeue_pending.py picks it up later.
    try:
        await run_in_threadpool(enqueue_inbound, queue, message.message_id)
    except Exception:
        logger.exception("Failed to enqueue inbound message id=%s", message.message_id)
        return empty_twiml()

    # Only move 'received' -> 'queued', so a fast worker's 'processed' isn't overwritten.
    await session.execute(
        update(InboundMessage)
        .where(InboundMessage.id == row_id, InboundMessage.status == "received")
        .values(status="queued")
    )
    await session.commit()

    return empty_twiml()


@app.post("/whatsapp/status")
async def whatsapp_status(
    request: Request, session: Annotated[AsyncSession, Depends(get_async_session)]
) -> Response:
    """Twilio delivery status callbacks for messages the bot sent."""
    params = await _form(request)
    if not _signature_ok(request, params):
        logger.warning("Rejected status callback with invalid Twilio signature")
        return Response(status_code=403)

    status = _TWILIO_STATUSES.get(params.get("MessageStatus", "").lower())
    sid = params.get("MessageSid", "")
    outbound = await session.scalar(
        select(OutboundMessage).where(OutboundMessage.twilio_sid == sid).with_for_update()
    )
    if outbound is None or status is None:
        return Response(status_code=204)  # not ours, or a status we don't track

    if status == "failed":
        if outbound.status != "read":
            outbound.status = "failed"
            code = params.get("ErrorCode", "")
            outbound.error_code = int(code) if code.isdigit() else None
            detail = _URLS.sub("<url>", params.get("ErrorMessage", "")).strip()
            outbound.error = f"Twilio delivery failed: error {code or '?'}" + (f": {detail}" if detail else "")
            logger.warning("Outbound id=%s failed to deliver: error %s", outbound.id, code or "?")
    elif _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(outbound.status, 0):
        outbound.status = status
    outbound.status_updated_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=204)
