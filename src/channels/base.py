import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

# The value stored in inbound_messages.channel and outbound_messages.channel.
TWILIO = "twilio"
META = "meta"
CHANNELS = (TWILIO, META)
ChannelName = Literal["twilio", "meta"]

# Provider errors meaning "outside WhatsApp's 24-hour window": the participant
# must write first. Meta 131047 (re-engagement message), Twilio 63016.
WINDOW_ERROR_CODES = {131047, 63016}

_URLS = re.compile(r"https?://\S+|/20\d\d-\d\d-\d\d/\S+")
_LONG_NUMBERS = re.compile(r"\+?\d[\d ]{6,}\d")


def clean_error_text(text: object) -> str:
    """Provider error text safe to store and log: no URLs or phone numbers."""
    cleaned = _URLS.sub("<url>", str(text or ""))
    return _LONG_NUMBERS.sub("<number>", cleaned).strip()


def error_details(**fields: object) -> dict[str, object]:
    """The provider's error as stored in outbound_messages.error_details (empty fields dropped)."""
    return {key: value for key, value in fields.items() if value not in (None, "")}


# =============
# MESSAGE MODEL
# =============
class MessageType(StrEnum):
    TEXT = "text"
    AUDIO = "audio"
    OTHER = "other"


@dataclass
class InboundMessage:
    message_id: str
    sender: str  # E.164, no "whatsapp:" prefix
    type: MessageType
    received_at: datetime
    text: str | None = None
    media_url: str | None = None
    media_content_type: str | None = None
    replied_to_message_id: str | None = None
    # Meta only: the media ID to download with (Twilio uses media_url).
    media_id: str | None = None
    # Meta only (None for Twilio, which doesn't say): True for a voice note
    # recorded in WhatsApp, False for an uploaded audio file.
    is_voice_note: bool | None = None
    # Meta only: "no", "forwarded" or "frequently_forwarded" (None for Twilio).
    forwarded: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def audio_origin(message: InboundMessage) -> str:
    """recorded | forwarded | uploaded | unknown, from the provider's flags.

    Forwarded wins (a forwarded voice note isn't the participant's own
    recording); then Meta's voice flag. Twilio says neither: unknown.
    """
    if message.forwarded in ("forwarded", "frequently_forwarded"):
        return "forwarded"
    if message.is_voice_note is True:
        return "recorded"
    if message.is_voice_note is False:
        return "uploaded"
    return "unknown"


@dataclass
class StatusUpdate:
    """A delivery status for a message the bot sent.

    status is one of sent, delivered, read, failed (anything else is None and
    ignored). error and details never include URLs or phone numbers.
    """

    message_id: str
    status: str | None
    error_code: int | None = None
    error: str | None = None
    details: dict[str, Any] | None = None


class MediaDownloadError(RuntimeError):
    """Media could not be fetched. The message never includes the media URL."""


class MessageSendError(RuntimeError):
    """The channel didn't accept an outbound message. Never includes URLs or numbers.

    permanent: retrying the same message won't help (e.g. an invalid request).
    details: the provider's error fields (code, title, subcode, fbtrace_id, ...),
    already cleaned of URLs and numbers, for outbound_messages.error_details.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        code: int | None = None,
        permanent: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.permanent = permanent
        self.details = details

    @property
    def outside_window(self) -> bool:
        """The participant hasn't written within 24 hours: retrying can't help."""
        return self.code in WINDOW_ERROR_CODES


class MediaDeleteError(RuntimeError):
    """The channel refused to delete media. The message never includes media URLs."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# =========================
# CHANNEL ADAPTER INTERFACE
# =========================
class ChannelAdapter(Protocol):
    name: str

    def validate_request(self, url: str, params: dict[str, str], signature: str) -> bool: ...

    def parse_inbound(self, params: dict[str, Any]) -> InboundMessage: ...

    def download_media(self, message: InboundMessage) -> bytes: ...

    def delete_media(self, message: InboundMessage) -> None: ...

    def send_text(self, to: str, body: str, status_callback: str | None = None) -> str: ...

    def send_media(
        self,
        to: str,
        media_url: str,
        caption: str | None = None,
        status_callback: str | None = None,
    ) -> str: ...
