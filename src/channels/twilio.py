from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from src.channels.base import (
    TWILIO,
    InboundMessage,
    MediaDeleteError,
    MediaDownloadError,
    MessageSendError,
    MessageType,
    StatusUpdate,
    clean_error_text,
    error_details,
)

WHATSAPP_PREFIX = "whatsapp:"


# Twilio's message statuses, mapped to ours; the rest aren't tracked.
_STATUSES = {
    "accepted": "sent",
    "queued": "sent",
    "sending": "sent",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
}


# =======
# HELPERS
# =======
def _strip_prefix(number: str) -> str:
    return number.removeprefix(WHATSAPP_PREFIX)


def _with_prefix(number: str) -> str:
    return number if number.startswith(WHATSAPP_PREFIX) else f"{WHATSAPP_PREFIX}{number}"


# ==============
# TWILIO CHANNEL
# ==============
class TwilioChannel:
    name = TWILIO

    def __init__(self, account_sid: str, auth_token: str, whatsapp_number: str) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._whatsapp_number = whatsapp_number
        self._validator = RequestValidator(auth_token)
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self._account_sid, self._auth_token)
        return self._client

    def validate_request(self, url: str, params: dict[str, str], signature: str) -> bool:
        return self._validator.validate(url, params, signature)

    def parse_inbound(self, params: dict[str, str]) -> InboundMessage:
        body = params.get("Body", "")
        media_url = params.get("MediaUrl0") or None
        media_content_type = params.get("MediaContentType0") or None

        if media_content_type and media_content_type.startswith("audio/"):
            message_type = MessageType.AUDIO
        elif media_content_type or not body:
            message_type = MessageType.OTHER
        else:
            message_type = MessageType.TEXT

        return InboundMessage(
            message_id=params.get("MessageSid", ""),
            sender=_strip_prefix(params.get("From", "")),
            type=message_type,
            received_at=datetime.now(UTC),
            text=body or None,
            media_url=media_url,
            media_content_type=media_content_type,
            replied_to_message_id=params.get("OriginalRepliedMessageSid") or None,
            raw=dict(params),
        )

    def parse_status(self, params: dict[str, str]) -> StatusUpdate:
        """A status callback (POST /whatsapp/status) for a message the bot sent."""
        status = _STATUSES.get(params.get("MessageStatus", "").lower())
        code: int | None = None
        error: str | None = None
        if status == "failed":
            raw_code = params.get("ErrorCode", "")
            code = int(raw_code) if raw_code.isdigit() else None
            detail = clean_error_text(params.get("ErrorMessage", ""))
            error = f"Twilio delivery failed: error {raw_code or '?'}" + (f": {detail}" if detail else "")
            details = error_details(provider="twilio", code=code, message=detail)
            return StatusUpdate(params.get("MessageSid", ""), status, code, error, details)
        return StatusUpdate(message_id=params.get("MessageSid", ""), status=status)

    def download_media(self, message: InboundMessage) -> bytes:
        if not message.media_url:
            raise ValueError(f"Message {message.message_id} has no media")
        # httpx errors embed the media URL; re-raise without it (and without
        # chaining) so it never reaches logs or tracebacks.
        try:
            response = httpx.get(
                message.media_url,
                auth=(self._account_sid, self._auth_token),
                follow_redirects=True,
                timeout=30.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise MediaDownloadError(
                f"Media download for {message.message_id} failed: HTTP {status}"
            ) from None
        except httpx.HTTPError as exc:
            raise MediaDownloadError(
                f"Media download for {message.message_id} failed: {type(exc).__name__}"
            ) from None
        return response.content

    def delete_media(self, message: InboundMessage) -> None:
        """Delete every media item on the message (MediaUrl0..N). Already gone counts as deleted.

        Raises MediaDeleteError with Twilio's status, error code and message
        (URLs removed) if Twilio refuses.
        """
        urls = [
            message.raw[key]
            for key in sorted(message.raw, key=lambda k: int(k[8:]) if k[8:].isdigit() else -1)
            if key.startswith("MediaUrl") and key[8:].isdigit() and message.raw[key]
        ] or ([message.media_url] if message.media_url else [])
        if not urls:
            raise ValueError(f"Message {message.message_id} has no media")
        for url in urls:
            media_sid = urlparse(url).path.rstrip("/").split("/")[-1]
            try:
                self.client.messages(message.message_id).media(media_sid).delete()
            except TwilioRestException as exc:
                if exc.status == 404:
                    continue  # already deleted
                detail = clean_error_text(exc.msg)
                raise MediaDeleteError(
                    f"Twilio refused to delete media for {message.message_id}: HTTP {exc.status},"
                    f" error {exc.code}: {detail}",
                    status=exc.status,
                    code=exc.code,
                ) from None

    def _create(self, to: str, status_callback: str | None, **fields: object) -> str:
        if status_callback:
            fields["status_callback"] = status_callback
        try:
            sent = self.client.messages.create(
                from_=_with_prefix(self._whatsapp_number), to=_with_prefix(to), **fields
            )
        except TwilioRestException as exc:
            detail = clean_error_text(exc.msg)
            # 4xx: the request itself is bad (invalid number, opted out, ...);
            # 429 and 5xx may succeed later.
            permanent = exc.status is not None and 400 <= exc.status < 500 and exc.status != 429
            raise MessageSendError(
                f"Twilio didn't accept the message: HTTP {exc.status}, error {exc.code}: {detail}",
                status=exc.status,
                code=exc.code,
                permanent=permanent,
                details=error_details(provider="twilio", http_status=exc.status, code=exc.code, message=detail),
            ) from None
        return sent.sid

    def send_text(self, to: str, body: str, status_callback: str | None = None) -> str:
        return self._create(to, status_callback, body=body)

    def send_media(
        self,
        to: str,
        media_url: str,
        caption: str | None = None,
        status_callback: str | None = None,
    ) -> str:
        fields: dict[str, object] = {"media_url": [media_url]}
        if caption:
            fields["body"] = caption
        return self._create(to, status_callback, **fields)
