"""Realistic Meta WhatsApp Cloud API webhook payloads (all values fake).

Shapes follow what Meta posts to the webhook for the "messages" field:
https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
"""

import base64
import hashlib
import hmac
import itertools
import json
from typing import Any

WABA_ID = "200000000000002"
PHONE_NUMBER_ID = "100000000000001"  # matches META_PHONE_NUMBER_ID in conftest
DISPLAY_NUMBER = "15550001111"
APP_SECRET = "test-app-secret"  # matches META_APP_SECRET in conftest
TIMESTAMP = "1793534400"  # 2026-11-01 12:00 UTC

_counter = itertools.count(1)


def wamid(wa_id: str) -> str:
    """A wamid like Meta's: base64 that embeds the participant's number."""
    n = next(_counter)
    raw = b"\x1c\x18\x0c" + wa_id.encode() + b"\x15\x02\x00\x12\x18\x14" + f"3EB0{n:016X}".encode()
    return "wamid." + base64.b64encode(raw).decode()


def media_id() -> str:
    return f"{1500000000000000 + next(_counter)}"


# ========
# MESSAGES
# ========
def text(wa_id: str, body: str, **extra: Any) -> dict[str, Any]:
    return {"from": wa_id, "id": wamid(wa_id), "timestamp": TIMESTAMP, "text": {"body": body}, "type": "text", **extra}


def voice_note(wa_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "from": wa_id,
        "id": wamid(wa_id),
        "timestamp": TIMESTAMP,
        "type": "audio",
        "audio": {
            "mime_type": "audio/ogg; codecs=opus",
            "sha256": "Qm9ndXNTaGEyNTZGb3JUZXN0cw==",
            "id": media_id(),
            "voice": True,
        },
        **extra,
    }


def audio_file(wa_id: str, **extra: Any) -> dict[str, Any]:
    """An audio file attached from the phone (not recorded in WhatsApp)."""
    return {
        "from": wa_id,
        "id": wamid(wa_id),
        "timestamp": TIMESTAMP,
        "type": "audio",
        "audio": {"mime_type": "audio/mpeg", "sha256": "QXVkaW9GaWxlU2hhMjU2", "id": media_id(), "voice": False},
        **extra,
    }


def image(wa_id: str, caption: str | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"mime_type": "image/jpeg", "sha256": "SW1hZ2VTaGEyNTY=", "id": media_id()}
    if caption:
        body["caption"] = caption
    return {"from": wa_id, "id": wamid(wa_id), "timestamp": TIMESTAMP, "type": "image", "image": body, **extra}


def reply_context(replied_to: str) -> dict[str, Any]:
    """context for a swipe-reply to one of the bot's messages."""
    return {"context": {"from": DISPLAY_NUMBER, "id": replied_to}}


def forwarded_context(frequently: bool = False) -> dict[str, Any]:
    context: dict[str, Any] = {"forwarded": True}
    if frequently:
        context["frequently_forwarded"] = True
    return {"context": context}


# ========
# STATUSES
# ========
def status(message_id: str, value: str, recipient: str) -> dict[str, Any]:
    result: dict[str, Any] = {"id": message_id, "status": value, "timestamp": TIMESTAMP, "recipient_id": recipient}
    if value in ("sent", "delivered"):
        result["conversation"] = {"id": "CONVERSATION_ID", "origin": {"type": "service"}}
        result["pricing"] = {"billable": True, "pricing_model": "PMP", "category": "service"}
    return result


def failed_status(message_id: str, recipient: str, code: int = 131047) -> dict[str, Any]:
    return {
        "id": message_id,
        "status": "failed",
        "timestamp": TIMESTAMP,
        "recipient_id": recipient,
        "errors": [
            {
                "code": code,
                "title": "Re-engagement message",
                "message": "Re-engagement message",
                "error_data": {
                    "details": "Message failed to send because more than 24 hours have passed since the"
                    " customer last replied to this number."
                },
                "href": "https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes/",
            }
        ],
    }


# ======
# EVENTS
# ======
def event(
    messages: list[dict[str, Any]] = (),
    statuses: list[dict[str, Any]] = (),
    name: str = "Chikondi Phiri",
    phone_number_id: str = PHONE_NUMBER_ID,
) -> dict[str, Any]:
    """One webhook POST body. Messages and statuses arrive in the same envelope."""
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": DISPLAY_NUMBER, "phone_number_id": phone_number_id},
    }
    if messages:
        senders = sorted({m["from"] for m in messages})
        value["contacts"] = [{"profile": {"name": name}, "wa_id": wa_id} for wa_id in senders]
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = list(statuses)
    return {"object": "whatsapp_business_account", "entry": [{"id": WABA_ID, "changes": [{"value": value, "field": "messages"}]}]}


def signed(payload: dict[str, Any], secret: str = APP_SECRET) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
