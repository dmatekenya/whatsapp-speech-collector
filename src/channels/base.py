from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


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
    raw: dict[str, Any] = field(default_factory=dict)


class MediaDownloadError(RuntimeError):
    """Media could not be fetched. The message never includes the media URL."""


class MessageSendError(RuntimeError):
    """The channel didn't accept an outbound message. Never includes URLs or numbers.

    permanent: retrying the same message won't help (e.g. an invalid request).
    """

    def __init__(
        self, message: str, status: int | None = None, code: int | None = None, permanent: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.permanent = permanent


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
    def validate_request(self, url: str, params: dict[str, str], signature: str) -> bool: ...

    def parse_inbound(self, params: dict[str, str]) -> InboundMessage: ...

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
