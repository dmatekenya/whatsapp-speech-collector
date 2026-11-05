"""Drive the real conversation engine with fake messages and a fake channel.

Used by scripts/chat_simulator.py and the scenario tests. Each call stores an
inbound message (and, for voice notes, a stored recording with the given
duration) the way the webhook and worker would, runs the engine, and sends
the reply through a channel that records messages instead of calling Twilio.
Never point it at the real database.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.channels.twilio import TwilioChannel
from src.conversation.engine import respond
from src.conversation.outbound import send_outbound
from src.storage.db import get_sync_sessionmaker
from src.storage.audio import apply_audio_origin
from src.storage.duplicates import relink_duplicates
from src.storage.models import InboundMessage, OutboundMessage, Participant, Recording

FAKE_ACCOUNT = "AC" + "0" * 32


@dataclass
class Sent:
    """One message the bot sent."""

    outbound_id: int
    body: str
    prompt_id: str | None  # set for image messages
    media_url: str | None
    sid: str

    @property
    def is_image(self) -> bool:
        return self.prompt_id is not None

    def __str__(self) -> str:
        header = f"[image {self.prompt_id}]\n" if self.is_image else ""
        return header + self.body


class RecordingChannel(TwilioChannel):
    """Parses like Twilio; sends into a list and returns fake message SIDs."""

    def __init__(self) -> None:
        super().__init__(FAKE_ACCOUNT, "fake-token", "+10000000000")
        self.sent: list[tuple[str, str, str | None, str | None]] = []  # (sid, to, body, url)

    def _fake_sid(self) -> str:
        return f"SM{uuid.uuid4().hex}"

    def send_text(self, to: str, body: str, status_callback: str | None = None) -> str:
        sid = self._fake_sid()
        self.sent.append((sid, to, body, None))
        return sid

    def send_media(
        self, to: str, media_url: str, caption: str | None = None, status_callback: str | None = None
    ) -> str:
        sid = self._fake_sid()
        self.sent.append((sid, to, caption, media_url))
        return sid


class ChatHarness:
    def __init__(self, phone_number: str, channel: RecordingChannel | None = None, start: datetime | None = None) -> None:
        self.phone_number = phone_number
        self.channel = channel or RecordingChannel()
        self.now = start or datetime.now(UTC)
        self.last_prompt_sid: str | None = None
        self.last_audio_hash: str | None = None
        self.factory = get_sync_sessionmaker()

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)

    # ========
    # MESSAGES
    # ========
    def text(self, body: str) -> list[Sent]:
        return self._turn("SM", {"Body": body, "NumMedia": "0"}, "text")

    def voice(
        self,
        seconds: float,
        reply: bool = False,
        duplicate: bool = False,
        reply_to: str | None = None,
        origin: str = "unknown",
        active_seconds: float | None = None,
    ) -> list[Sent]:
        """A voice note. origin: recorded, forwarded, uploaded or unknown (Twilio).
        active_seconds: as the worker measures it (None: not measured, no speech check)."""
        payload = {"Body": "", "NumMedia": "1", "MediaContentType0": "audio/ogg"}
        if reply_to or (reply and self.last_prompt_sid):
            payload["OriginalRepliedMessageSid"] = reply_to or self.last_prompt_sid
        return self._turn(
            "MM", payload, "audio", seconds=seconds, duplicate=duplicate, origin=origin, active_seconds=active_seconds
        )

    def image(self) -> list[Sent]:
        return self._turn("MM", {"Body": "", "NumMedia": "1", "MediaContentType0": "image/jpeg"}, "other")

    def media(self, content_type: str) -> list[Sent]:
        return self._turn("MM", {"Body": "", "NumMedia": "1", "MediaContentType0": content_type}, "other")

    # ======
    # A TURN
    # ======
    def _store(
        self,
        prefix: str,
        payload: dict[str, str],
        message_type: str,
        seconds: float | None,
        duplicate: bool,
        origin: str = "unknown",
        active_seconds: float | None = None,
    ) -> str:
        sid = f"{prefix}{uuid.uuid4().hex}"
        payload = {"MessageSid": sid, "From": f"whatsapp:{self.phone_number}", **payload}
        if payload.get("NumMedia") == "1":
            payload["MediaUrl0"] = f"https://api.twilio.com/2010-04-01/Accounts/{FAKE_ACCOUNT}/Messages/{sid}/Media/ME{uuid.uuid4().hex}"
        with self.factory() as db:
            participant = db.scalar(select(Participant).where(Participant.phone_number == self.phone_number))
            message = InboundMessage(
                message_sid=sid,
                sender=self.phone_number,
                message_type=message_type,
                raw_payload=payload,
                status="processed",
                participant_id=participant.participant_id if participant else None,
                received_at=self.now,
            )
            db.add(message)
            db.flush()
            if message_type == "audio" and participant is not None:
                digest = self.last_audio_hash if (duplicate and self.last_audio_hash) else hashlib.sha256(sid.encode()).hexdigest()
                self.last_audio_hash = digest
                recording = Recording(
                    inbound_message_id=message.id,
                    participant_id=participant.participant_id,
                    status="stored",
                    duration_s=seconds,
                    active_seconds=active_seconds,
                    content_sha256=digest,
                )
                apply_audio_origin(recording, origin)  # as the worker does
                db.add(recording)
                db.flush()
                relink_duplicates(db, digest)
            db.commit()
        return sid

    def _turn(
        self,
        prefix: str,
        payload: dict[str, str],
        message_type: str,
        seconds: float | None = None,
        duplicate: bool = False,
        origin: str = "unknown",
        active_seconds: float | None = None,
    ) -> list[Sent]:
        sid = self._store(prefix, payload, message_type, seconds, duplicate, origin, active_seconds)
        pending = respond(sid, now=self.now)
        if pending:
            send_outbound(pending[0], self.channel, self.now)
        with self.factory() as db:
            inbound_id = db.scalar(select(InboundMessage.id).where(InboundMessage.message_sid == sid))
            rows = db.scalars(
                select(OutboundMessage).where(OutboundMessage.inbound_message_id == inbound_id).order_by(OutboundMessage.sequence)
            ).all()
        urls = {sent_sid: url for sent_sid, _, _, url in self.channel.sent}
        sent = [
            Sent(row.id, row.body, row.media_prompt_id, urls.get(row.provider_message_id), row.provider_message_id or "")
            for row in rows
        ]
        for row in rows:
            if row.assignment_id is not None and row.provider_message_id:
                self.last_prompt_sid = row.provider_message_id
        return sent
