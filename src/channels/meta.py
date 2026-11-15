"""The Meta WhatsApp Cloud API channel (Graph API).

Same interface as TwilioChannel. One webhook call from Meta can carry several
messages and statuses; parse_event() splits it. Each message is stored on its
own (see src/api/meta.py) as {"message": {...}, "metadata": {...}}, which is
what parse_inbound() reads back in the worker.

Errors and logs never include tokens, media URLs, phone numbers or message text.
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from src.channels.base import (
    META,
    InboundMessage,
    MediaDownloadError,
    MessageSendError,
    MessageType,
    StatusUpdate,
    clean_error_text,
    error_details,
)

GRAPH_URL = "https://graph.facebook.com"
# Rate and throughput limits: worth retrying later, unlike other 4xx errors.
# https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
TRANSIENT_ERROR_CODES = {1, 2, 4, 80007, 130429, 131000, 131016, 131056}
STATUSES = {"sent": "sent", "delivered": "delivered", "read": "read", "failed": "failed"}
# Message types sent as a media object with an "id" and "mime_type".
MEDIA_TYPES = ("audio", "image", "video", "document", "sticker")


# =======
# HELPERS
# =======
def to_e164(wa_id: str) -> str:
    """Meta's wa_id (digits, no "+") as E.164, so participant lookup is unchanged."""
    digits = re.sub(r"\D", "", wa_id or "")
    return f"+{digits}" if digits else ""


def to_wa_id(number: str) -> str:
    """E.164 (+265...) as the digits-only form the Graph API takes."""
    return re.sub(r"\D", "", number or "")


def _forwarded(context: dict[str, Any]) -> str:
    if context.get("frequently_forwarded"):
        return "frequently_forwarded"
    if context.get("forwarded"):
        return "forwarded"
    return "no"


def _received_at(message: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(message["timestamp"]), UTC)
    except (KeyError, TypeError, ValueError):
        return datetime.now(UTC)


def _changes(payload: Any) -> list[dict[str, Any]]:
    """Every change.value in a webhook payload that's a dict."""
    values = []
    entries = payload.get("entry") if isinstance(payload, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        for change in changes if isinstance(changes, list) else []:
            value = change.get("value") if isinstance(change, dict) else None
            if isinstance(value, dict):
                values.append(value)
    return values


# ============
# META CHANNEL
# ============
class MetaChannel:
    name = META

    def __init__(
        self,
        access_token: str | None,
        phone_number_id: str | None,
        graph_version: str = "v23.0",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._access_token = access_token or None
        self._phone_number_id = phone_number_id or None
        self._graph_version = graph_version
        # Tests pass an httpx.MockTransport; nothing else should.
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {self._access_token}"},
        )

    def _graph(self, path: str) -> str:
        return f"{GRAPH_URL}/{self._graph_version}/{path}"

    # =======
    # INBOUND
    # =======
    def validate_request(self, url: str, params: dict[str, str], signature: str) -> bool:
        """Not used: Meta signs the raw body, which src/api/meta.py checks."""
        return False

    def parse_event(self, payload: Any) -> tuple[list[dict[str, Any]], list[StatusUpdate]]:
        """Split a webhook payload into messages to store and delivery statuses.

        Each message is returned in the form it is stored in
        inbound_messages.raw_payload: {"message": {...}, "metadata": {...}}.
        Contact profile names are dropped (the registry has the names).
        """
        messages: list[dict[str, Any]] = []
        statuses: list[StatusUpdate] = []
        for value in _changes(payload):
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            for message in value.get("messages") or []:
                if isinstance(message, dict) and message.get("id") and message.get("from"):
                    messages.append({"message": message, "metadata": metadata})
            for status in value.get("statuses") or []:
                if isinstance(status, dict) and status.get("id"):
                    statuses.append(self.parse_status(status))
        return messages, statuses

    def parse_inbound(self, params: dict[str, Any]) -> InboundMessage:
        message = params.get("message", {}) if isinstance(params, dict) else {}
        kind = message.get("type") or ""
        context = message.get("context") if isinstance(message.get("context"), dict) else {}
        media = message.get(kind) if kind in MEDIA_TYPES and isinstance(message.get(kind), dict) else {}
        content_type = media.get("mime_type") or None

        text: str | None = None
        is_voice_note: bool | None = None
        if kind == "text":
            text, message_type = (message.get("text") or {}).get("body") or None, MessageType.TEXT
        elif kind == "button":
            # A tap on a template's quick-reply button: treated like typing its label.
            text, message_type = (message.get("button") or {}).get("text") or None, MessageType.TEXT
        elif kind == "interactive":
            interactive = message.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            text, message_type = reply.get("title") or None, MessageType.TEXT
        elif kind == "audio" or (kind == "document" and (content_type or "").startswith("audio/")):
            message_type = MessageType.AUDIO
            is_voice_note = bool(media.get("voice", False))
        else:
            message_type = MessageType.OTHER
            text = media.get("caption") or None
        if message_type == MessageType.TEXT and not text:
            message_type = MessageType.OTHER

        return InboundMessage(
            message_id=message.get("id", ""),
            sender=to_e164(message.get("from", "")),
            type=message_type,
            received_at=_received_at(message),
            text=text,
            media_content_type=content_type,
            media_id=media.get("id") or None,
            replied_to_message_id=context.get("id") or None,
            is_voice_note=is_voice_note,
            forwarded=_forwarded(context),
            raw=dict(params),
        )

    def parse_status(self, status: dict[str, Any]) -> StatusUpdate:
        value = STATUSES.get(str(status.get("status", "")).lower())
        code: int | None = None
        error: str | None = None
        if value == "failed":
            errors = status.get("errors") or [{}]
            first = errors[0] if isinstance(errors[0], dict) else {}
            raw_code = first.get("code")
            code = int(raw_code) if str(raw_code).isdigit() else None
            title = clean_error_text(first.get("title") or first.get("message") or "")
            error = f"Meta delivery failed: error {code or '?'}" + (f": {title}" if title else "")
            details = error_details(
                provider="meta",
                code=code,
                title=title,
                message=clean_error_text(first.get("message")),
                details=clean_error_text((first.get("error_data") or {}).get("details")),
            )
            return StatusUpdate(str(status.get("id", "")), value, code, error, details)
        return StatusUpdate(message_id=str(status.get("id", "")), status=value)

    # =====
    # MEDIA
    # =====
    def download_media(self, message: InboundMessage) -> bytes:
        """GET /{media-id} for a short-lived URL, then fetch it with the token."""
        if not message.media_id:
            raise ValueError("Message has no media")
        if not self._access_token:
            raise MediaDownloadError(f"Media download failed: META_ACCESS_TOKEN is not set")
        # httpx errors embed the URL; re-raise without it (and without chaining).
        try:
            with self._client() as client:
                info = client.get(self._graph(message.media_id))
                info.raise_for_status()
                details = info.json()
                response = client.get(details["url"], follow_redirects=True)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MediaDownloadError(
                f"Media download failed: HTTP {exc.response.status_code}"
            ) from None
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise MediaDownloadError(
                f"Media download failed: {type(exc).__name__}"
            ) from None
        expected = details.get("sha256")
        if expected and hashlib.sha256(response.content).hexdigest() != expected:
            raise MediaDownloadError(f"Media download failed: checksum mismatch")
        return response.content

    def delete_media(self, message: InboundMessage) -> None:
        """Nothing to do: Meta keeps received media only for a limited time."""

    # =======
    # SENDING
    # =======
    def _send(self, to: str, fields: dict[str, Any]) -> str:
        if not self._access_token or not self._phone_number_id:
            raise MessageSendError(
                "Meta isn't configured: set META_ACCESS_TOKEN and META_PHONE_NUMBER_ID", permanent=True
            )
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_wa_id(to), **fields}
        try:
            with self._client() as client:
                response = client.post(self._graph(f"{self._phone_number_id}/messages"), json=body)
        except httpx.HTTPError as exc:
            raise MessageSendError(f"Meta didn't accept the message: {type(exc).__name__}") from None

        if response.is_success:
            try:
                return response.json()["messages"][0]["id"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise MessageSendError("Meta accepted the message but returned no message ID") from None

        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        if not isinstance(error, dict):
            error = {}
        code = error.get("code") if isinstance(error.get("code"), int) else None
        detail = clean_error_text(error.get("message", ""))
        status = response.status_code
        permanent = 400 <= status < 500 and status != 429 and code not in TRANSIENT_ERROR_CODES
        error_data = error.get("error_data") if isinstance(error.get("error_data"), dict) else {}
        raise MessageSendError(
            f"Meta didn't accept the message: HTTP {status}, error {code}: {detail}",
            status=status,
            code=code,
            permanent=permanent,
            details=error_details(
                provider="meta",
                http_status=status,
                code=code,
                subcode=error.get("error_subcode"),
                type=clean_error_text(error.get("type")),
                title=clean_error_text(error.get("error_user_title")),
                message=detail,
                details=clean_error_text(error_data.get("details")),
                fbtrace_id=clean_error_text(error.get("fbtrace_id")),
            ),
        )

    def send_text(self, to: str, body: str, status_callback: str | None = None) -> str:
        # status_callback is Twilio's; Meta posts statuses to the webhook.
        return self._send(to, {"type": "text", "text": {"body": body, "preview_url": False}})

    def send_media(
        self,
        to: str,
        media_url: str,
        caption: str | None = None,
        status_callback: str | None = None,
    ) -> str:
        image: dict[str, Any] = {"link": media_url}
        if caption:
            image["caption"] = caption
        return self._send(to, {"type": "image", "image": image})

    def send_template(self, to: str, name: str, language: str, body_params: list[str]) -> str:
        """An approved message template, which WhatsApp allows outside the 24-hour window.

        body_params fill the body's {{1}}, {{2}}, ... in order. A template that isn't
        approved, or a wrong name, language or parameter count, comes back as a
        permanent MessageSendError with Meta's code (e.g. 132001, 132000).
        """
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if body_params:
            template["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": str(value)} for value in body_params]}
            ]
        return self._send(to, {"type": "template", "template": template})
