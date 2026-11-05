"""Meta WhatsApp Cloud API webhook.

Receives webhook calls from Meta at /webhooks/meta, alongside the Twilio
webhook. GET answers Meta's subscription check. POST checks the signature,
then stores each message in inbound_messages (channel "meta", deduplicated on
the wamid) and queues it for the worker, exactly like the Twilio webhook, and
applies delivery statuses to outbound_messages.

Never logs message text, media, phone numbers, WhatsApp IDs, tokens or secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from rq import Queue
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.inbound import apply_status, store_and_enqueue
from src.channels.base import META
from src.channels.factory import get_channel
from src.storage.db import get_async_session
from src.storage.queue import get_inbound_queue

# =============
# CONFIGURATION
# =============

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/meta", tags=["meta"])

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


@dataclass(frozen=True)
class MetaWebhookConfig:
    verify_token: str | None
    app_secret: str | None
    # When set, messages sent to any other number are ignored (not stored).
    phone_number_id: str | None = None


def _secret_value(value: Any) -> str | None:
    """Return a plain string from a str or pydantic SecretStr; None if empty."""
    if value is None:
        return None
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    value = str(value).strip()
    return value or None


def get_meta_config() -> MetaWebhookConfig:
    """Read META_VERIFY_TOKEN, META_APP_SECRET and META_PHONE_NUMBER_ID from the app settings."""
    from src.config import get_settings

    settings = get_settings()
    return MetaWebhookConfig(
        verify_token=_secret_value(settings.meta_verify_token),
        app_secret=_secret_value(settings.meta_app_secret),
        phone_number_id=settings.meta_phone_number_id or None,
    )


# ====================
# ACCESS LOG REDACTION
# ====================

# Meta sends the verify token in the query string, as both hub.verify_token
# and hub_verify_token; uvicorn's access log would otherwise write it out in full.
_TOKEN_IN_URL = re.compile(r"(hub[._]verify_token=)[^&\s\"]*")


class RedactVerifyToken(logging.Filter):
    """Replace hub.verify_token and hub_verify_token values in log messages with [redacted]."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_URL.sub(r"\1[redacted]", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_URL.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def install_log_redaction() -> None:
    """Attach the redaction filter to uvicorn's access logger (safe to call twice)."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactVerifyToken) for f in access_logger.filters):
        access_logger.addFilter(RedactVerifyToken())


install_log_redaction()


# ===============
# SIGNATURE CHECK
# ===============


def signature_is_valid(raw_body: bytes, header_value: str | None, app_secret: str) -> bool:
    """Check X-Hub-Signature-256 ("sha256=<hex>") against the raw request body."""
    if not header_value or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    received = header_value[len(SIGNATURE_PREFIX):].strip().lower()
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


# ==============
# EVENT METADATA
# ==============


def summarize_event(payload: Any) -> dict[str, Any]:
    """Reduce a webhook payload to counts and types only.

    Deliberately ignores message bodies, media IDs, phone numbers, WhatsApp
    IDs, contact names and anything else that could identify a person.
    """
    summary: dict[str, Any] = {
        "object": None,
        "entries": 0,
        "fields": Counter(),
        "messages": 0,
        "message_types": Counter(),
        "statuses": 0,
        "status_values": Counter(),
        "errors": 0,
    }
    if not isinstance(payload, dict):
        return _plain(summary)

    obj = payload.get("object")
    summary["object"] = obj if isinstance(obj, str) else None

    entries = payload.get("entry")
    if not isinstance(entries, list):
        return _plain(summary)
    summary["entries"] = len(entries)

    for entry in entries:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            field = change.get("field")
            if isinstance(field, str):
                summary["fields"][field] += 1
            value = change.get("value")
            if not isinstance(value, dict):
                continue

            messages = value.get("messages")
            if isinstance(messages, list):
                summary["messages"] += len(messages)
                for message in messages:
                    if isinstance(message, dict) and isinstance(message.get("type"), str):
                        summary["message_types"][message["type"]] += 1

            statuses = value.get("statuses")
            if isinstance(statuses, list):
                summary["statuses"] += len(statuses)
                for status in statuses:
                    if isinstance(status, dict) and isinstance(status.get("status"), str):
                        summary["status_values"][status["status"]] += 1

            errors = value.get("errors")
            if isinstance(errors, list):
                summary["errors"] += len(errors)

    return _plain(summary)


def _plain(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: dict(v) if isinstance(v, Counter) else v for k, v in summary.items()}


# ======
# ROUTES
# ======


@router.get("")
async def verify_subscription(
    request: Request,
    config: MetaWebhookConfig = Depends(get_meta_config),
) -> Response:
    """Answer Meta's subscription check: echo hub.challenge if the token matches."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if not config.verify_token:
        logger.error("Meta webhook verification refused: META_VERIFY_TOKEN is not set")
        return Response(status_code=403)

    token_ok = token is not None and hmac.compare_digest(
        token.encode("utf-8"), config.verify_token.encode("utf-8")
    )
    if mode == "subscribe" and token_ok and challenge is not None:
        logger.info("Meta webhook verified")
        return PlainTextResponse(challenge)

    logger.warning("Meta webhook verification refused (mode=%s)", mode if mode == "subscribe" else "other")
    return Response(status_code=403)


@router.post("")
async def receive_event(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    queue: Annotated[Queue, Depends(get_inbound_queue)],
    config: MetaWebhookConfig = Depends(get_meta_config),
) -> Response:
    """Receive a Meta webhook event: check the signature, store and queue its
    messages, apply its statuses, return 200.

    A database error returns 500, so Meta retries; messages already stored
    are deduplicated on their wamid.
    """
    if not config.app_secret:
        logger.error("Meta webhook event refused: META_APP_SECRET is not set")
        return Response(status_code=503)

    raw_body = await request.body()
    if not signature_is_valid(raw_body, request.headers.get(SIGNATURE_HEADER), config.app_secret):
        logger.warning("Meta webhook event refused: invalid or missing signature (%d bytes)", len(raw_body))
        return Response(status_code=403)

    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        # Signed by Meta but unreadable: acknowledge so Meta does not retry forever.
        logger.warning("Meta webhook event received with unreadable JSON (%d bytes)", len(raw_body))
        return Response(status_code=200)

    summary = summarize_event(payload)
    logger.info(
        "Meta webhook event received: object=%s entries=%d fields=%s messages=%d types=%s "
        "statuses=%d status_values=%s errors=%d",
        summary["object"],
        summary["entries"],
        summary["fields"],
        summary["messages"],
        summary["message_types"],
        summary["statuses"],
        summary["status_values"],
        summary["errors"],
    )

    channel = get_channel(META)
    messages, statuses = channel.parse_event(payload)
    for stored in messages:
        to_number = stored.get("metadata", {}).get("phone_number_id")
        if config.phone_number_id and to_number and to_number != config.phone_number_id:
            # Another number on the same app: replies would come from ours.
            logger.warning("Meta message for a different phone number ID ignored (not META_PHONE_NUMBER_ID)")
            continue
        await store_and_enqueue(session, queue, META, channel.parse_inbound(stored))
    for status in statuses:
        await apply_status(session, META, status)
    return Response(status_code=200)
