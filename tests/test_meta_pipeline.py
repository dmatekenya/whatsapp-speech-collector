"""Meta end to end: webhook -> inbound_messages -> worker -> recording and replies.

Also routing: every reply goes out on the channel the triggering message
arrived on, whatever WHATSAPP_PROVIDER says.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from rq import Queue
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from src.channels.meta import MetaChannel
from src.config import get_settings
from src.conversation.harness import RecordingChannel
from src.conversation.outbound import send_outbound
from src.storage import db
from src.storage.models import Assignment, InboundMessage, OutboundMessage, Prompt, Recording
from src.tasks.config import CollectionConfig
from src.workers.jobs import process_inbound
from tests import meta_payloads as mp
from tests.conftest import REGISTERED_SENDER, add_participant

WA_ID = REGISTERED_SENDER.removeprefix("+")


# ========
# CHANNELS
# ========
class TwilioRecorder(RecordingChannel):
    """Parses like Twilio; serves fixed audio and records sends and deletes."""

    def __init__(self, media: bytes) -> None:
        super().__init__()
        self.media = media
        self.downloads = 0
        self.deletes = 0

    def download_media(self, message):  # noqa: ANN001, ANN201
        self.downloads += 1
        return self.media

    def delete_media(self, message) -> None:  # noqa: ANN001
        self.deletes += 1


class MetaRecorder(MetaChannel):
    """Parses like Meta; serves fixed audio and records sends and deletes."""

    def __init__(self, media: bytes) -> None:
        super().__init__("test-meta-access-token", mp.PHONE_NUMBER_ID)
        self.media = media
        self.sent: list[tuple[str, str, str | None, str | None]] = []  # (id, to, body, url)
        self.downloads = 0
        self.deletes = 0

    def download_media(self, message):  # noqa: ANN001, ANN201
        assert message.media_id
        self.downloads += 1
        return self.media

    def delete_media(self, message) -> None:  # noqa: ANN001
        self.deletes += 1

    def send_text(self, to: str, body: str, status_callback: str | None = None) -> str:
        assert status_callback is None  # Meta posts statuses to its webhook
        return self._record(to, body, None)

    def send_media(self, to: str, media_url: str, caption: str | None = None, status_callback: str | None = None) -> str:
        assert status_callback is None
        return self._record(to, caption, media_url)

    def _record(self, to: str, body: str | None, url: str | None) -> str:
        message_id = mp.wamid(to.removeprefix("+"))
        self.sent.append((message_id, to, body, url))
        return message_id


class Channels:
    def __init__(self, media: bytes) -> None:
        self.twilio = TwilioRecorder(media)
        self.meta = MetaRecorder(media)

    def get(self, name: str | None = None) -> RecordingChannel | MetaRecorder:
        return {"twilio": self.twilio, "meta": self.meta}[name or get_settings().whatsapp_provider]


def config() -> CollectionConfig:
    return CollectionConfig.model_validate(
        {
            "task_types": {
                t: {"prompted": True, "speakers_per_prompt": 5, "speech_type": "scripted" if t == "sentence" else "spontaneous"}
                for t in ("sentence", "image", "conversation_starter")
            },
            "session_size": 3,
            "assignment_expiry_hours": 24,
            "max_retakes_per_prompt": 2,
            "targets_minutes": {"per_participant": 30, "split": {"sentence": 1 / 3, "image": 1 / 3, "conversation_starter": 1 / 3}},
            # The test audio is 2 seconds long.
            "clip_seconds": {"min": 1, "max": 30},
        }
    )


# ========
# FIXTURES
# ========
@pytest.fixture
def channels(
    client: TestClient, opus_bytes: bytes, audio_root, fake_queue: Queue, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> Channels:
    """A registered participant, prompts, and the worker wired to recording channels.

    Sends happen straight away (no Redis), through the same routing as production.
    """
    add_participant("P0001", REGISTERED_SENDER, name="Chisomo Banda")
    with db.get_sync_sessionmaker()() as session:
        for n in range(1, 11):
            text = f"Sentence number {n} for reading aloud today."
            session.add(Prompt(prompt_id=f"S{n:05d}", prompt_type="sentence", text=text, normalized_text=text.lower(), source_file="t"))
        session.commit()
    current = config()
    monkeypatch.setattr("src.conversation.engine.get_collection_config", lambda: current)

    found = Channels(opus_bytes)
    monkeypatch.setattr("src.workers.jobs.get_channel", found.get)
    monkeypatch.setattr("src.conversation.outbound.get_channel", found.get)
    monkeypatch.setattr("src.workers.jobs.enqueue_send", lambda outbound_id: send_outbound(outbound_id))
    monkeypatch.setattr("src.conversation.outbound.enqueue_send", lambda outbound_id: send_outbound(outbound_id) and True)
    monkeypatch.setattr("src.workers.jobs.get_inbound_queue", lambda: fake_queue)
    return found


def from_meta(client: TestClient, message: dict) -> str:
    body, headers = mp.signed(mp.event([message]))
    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 200
    process_inbound(message["id"])
    return message["id"]


def from_twilio(client: TestClient, **params: str) -> str:
    sid = f"{'MM' if params.get('NumMedia') == '1' else 'SM'}{uuid.uuid4().hex}"
    params = {"MessageSid": sid, "From": f"whatsapp:{REGISTERED_SENDER}", "Body": "", "NumMedia": "0", **params}
    if params["NumMedia"] == "1":
        params.setdefault("MediaContentType0", "audio/ogg")
        params["MediaUrl0"] = f"https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/{sid}/Media/ME1"
    settings = get_settings()
    url = settings.public_base_url.rstrip("/") + "/whatsapp/webhook"
    signature = RequestValidator(settings.twilio_auth_token).compute_signature(url, params)
    assert client.post("/whatsapp/webhook", data=params, headers={"X-Twilio-Signature": signature}).status_code == 200
    process_inbound(sid)
    return sid


def outbound_rows() -> list[OutboundMessage]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(OutboundMessage).order_by(OutboundMessage.id)))


def recordings() -> list[Recording]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Recording).order_by(Recording.id)))


def assignments() -> list[Assignment]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Assignment).order_by(Assignment.id)))


# =======
# WEBHOOK
# =======
def test_batched_event_stores_and_queues_each_message_once(client: TestClient, fake_queue: Queue) -> None:
    other = "265881112223"
    messages = [mp.text(WA_ID, "Moni"), mp.voice_note(WA_ID), mp.image(other, caption="Chithunzi")]
    body, headers = mp.signed(mp.event(messages))

    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 200
    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 200  # Meta retried

    with db.get_sync_sessionmaker()() as session:
        rows = list(session.scalars(select(InboundMessage).order_by(InboundMessage.id)))
    assert [(r.channel, r.message_sid, r.sender, r.message_type) for r in rows] == [
        ("meta", messages[0]["id"], REGISTERED_SENDER, "text"),
        ("meta", messages[1]["id"], REGISTERED_SENDER, "audio"),
        ("meta", messages[2]["id"], "+265881112223", "other"),
    ]
    assert rows[1].raw_payload == {"message": messages[1], "metadata": {"display_phone_number": mp.DISPLAY_NUMBER, "phone_number_id": mp.PHONE_NUMBER_ID}}
    assert all(r.status == "queued" for r in rows)
    # Queued in arrival order, once each; job descriptions (logged by RQ) hide the wamid.
    assert [job.args for job in fake_queue.jobs] == [(m["id"],) for m in messages]
    assert all("wamid." not in job.description for job in fake_queue.jobs)


def test_message_to_another_phone_number_is_ignored(client: TestClient, fake_queue: Queue) -> None:
    body, headers = mp.signed(mp.event([mp.text(WA_ID, "Moni")], phone_number_id="999999999999999"))
    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 200
    with db.get_sync_sessionmaker()() as session:
        assert session.scalar(select(InboundMessage)) is None
    assert fake_queue.jobs == []


# =================
# DELIVERY STATUSES
# =================
def _outbound(channel: str, provider_message_id: str, status: str = "sent") -> int:
    with db.get_sync_sessionmaker()() as session:
        row = OutboundMessage(purpose=f"test-{uuid.uuid4().hex}", body="x", status=status, channel=channel,
                              provider_message_id=provider_message_id, sent_at=datetime.now(UTC))  # fmt: skip
        session.add(row)
        session.commit()
        return row.id


def _row(row_id: int) -> OutboundMessage:
    with db.get_sync_sessionmaker()() as session:
        return session.get(OutboundMessage, row_id)


def _post_statuses(client: TestClient, *statuses: dict) -> None:
    body, headers = mp.signed(mp.event(statuses=list(statuses)))
    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 200


def test_statuses_move_forward_only(client: TestClient) -> None:
    out = mp.wamid(WA_ID)
    row = _outbound("meta", out)
    _post_statuses(client, mp.status(out, "delivered", WA_ID))
    assert _row(row).status == "delivered"
    _post_statuses(client, mp.status(out, "read", WA_ID), mp.status(out, "delivered", WA_ID))  # late "delivered"
    assert _row(row).status == "read"
    _post_statuses(client, mp.failed_status(out, WA_ID))  # never replaces "read"
    assert _row(row).status == "read" and _row(row).error_code is None


def test_failed_status_stores_the_error_code(client: TestClient) -> None:
    out = mp.wamid(WA_ID)
    row = _outbound("meta", out)
    _post_statuses(client, mp.failed_status(out, WA_ID, code=131026))
    failed = _row(row)
    assert failed.status == "failed" and failed.error_code == 131026
    assert failed.error == "Meta delivery failed: error 131026: Re-engagement message"
    assert failed.error_details["code"] == 131026 and "fbtrace_id" not in failed.error_details


def test_outside_window_status_is_skipped_window(client: TestClient) -> None:
    out = mp.wamid(WA_ID)
    row = _outbound("meta", out)
    _post_statuses(client, mp.failed_status(out, WA_ID, code=131047))
    skipped = _row(row)
    assert skipped.status == "skipped_window" and skipped.error_code == 131047
    assert skipped.error_details["details"].startswith("Message failed to send because more than 24 hours")
    _post_statuses(client, mp.status(out, "delivered", WA_ID))  # a late status never revives it
    assert _row(row).status == "skipped_window"


def test_meta_statuses_never_touch_twilio_rows(client: TestClient) -> None:
    shared_id = "SAME-ID"
    twilio_row = _outbound("twilio", shared_id)
    meta_row = _outbound("meta", shared_id)
    _post_statuses(client, mp.status(shared_id, "read", WA_ID))
    assert _row(meta_row).status == "read" and _row(twilio_row).status == "sent"


# ====================
# RECORDINGS FROM META
# ====================
def test_meta_voice_note_is_stored_and_flagged(client: TestClient, channels: Channels, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "twilio_delete_media", True)  # still nothing to delete on Meta
    sid = from_meta(client, mp.voice_note(WA_ID))

    (recording,) = recordings()
    assert recording.status == "stored" and recording.original_path.endswith(".ogg")
    assert recording.is_voice_note is True and recording.forwarded == "no"
    assert recording.audio_origin == "recorded" and recording.qc_status == "pending"
    assert recording.provider_media_deleted is False
    assert channels.meta.downloads == 1 and channels.meta.deletes == 0
    assert channels.twilio.downloads == 0 and channels.twilio.sent == []
    with db.get_sync_sessionmaker()() as session:
        message = session.scalar(select(InboundMessage).where(InboundMessage.message_sid == sid))
    assert message.status == "processed" and message.media_deleted_at is None


def test_forwarded_audio_file_is_flagged(client: TestClient, channels: Channels) -> None:
    from_meta(client, mp.audio_file(WA_ID, **mp.forwarded_context(frequently=True)))
    (recording,) = recordings()
    assert recording.is_voice_note is False and recording.forwarded == "frequently_forwarded"
    # Forwarded wins over uploaded: never used.
    assert recording.audio_origin == "forwarded"
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")


def test_uploaded_audio_file_outside_a_session_is_rejected(client: TestClient, channels: Channels) -> None:
    from_meta(client, mp.audio_file(WA_ID))
    (recording,) = recordings()
    assert recording.audio_origin == "uploaded" and recording.original_content_type == "audio/mpeg"
    # Not yet matched to a prompt: the global rule (accepted only in free speech).
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")


def test_twilio_recordings_leave_the_meta_flags_empty(client: TestClient, channels: Channels) -> None:
    from_twilio(client, NumMedia="1")
    (recording,) = recordings()
    assert recording.status == "stored"
    assert recording.is_voice_note is None and recording.forwarded is None
    assert recording.audio_origin == "unknown" and recording.qc_status == "pending"


def test_forwarded_meta_voice_note_while_a_sentence_is_open(client: TestClient, channels: Channels) -> None:
    from_meta(client, mp.text(WA_ID, "Hi"))
    from_meta(client, mp.text(WA_ID, "1"))
    from_meta(client, mp.voice_note(WA_ID, **mp.forwarded_context()))

    reply = channels.meta.sent[-1][2]
    assert reply.startswith("That voice note was forwarded.") and "Sentence 1 of 3:" in reply
    (first,) = assignments()
    assert first.status == "sent" and first.retake_count == 0
    (recording,) = recordings()
    assert recording.assignment_id == first.id and recording.qc_reason == "not_recorded"


def test_uploaded_audio_file_while_a_sentence_is_open(client: TestClient, channels: Channels) -> None:
    from_meta(client, mp.text(WA_ID, "Hi"))
    from_meta(client, mp.text(WA_ID, "1"))
    from_meta(client, mp.audio_file(WA_ID))

    reply = channels.meta.sent[-1][2]
    assert reply.startswith("That looks like an audio file rather than a voice note.") and "Sentence 1 of 3:" in reply
    (first,) = assignments()
    assert first.status == "sent" and first.retake_count == 0
    (recording,) = recordings()
    assert recording.assignment_id == first.id and recording.qc_reason == "not_recorded"


# =======
# ROUTING
# =======
def test_meta_conversation_is_answered_only_via_meta(client: TestClient, channels: Channels) -> None:
    from_meta(client, mp.text(WA_ID, "Hi"))
    from_meta(client, mp.text(WA_ID, "1"))
    from_meta(client, mp.voice_note(WA_ID))

    rows = outbound_rows()
    assert len(rows) == 3 and all(r.channel == "meta" and r.status == "sent" for r in rows)
    assert [r.provider_message_id for r in rows] == [sent[0] for sent in channels.meta.sent]
    assert all(to == REGISTERED_SENDER for _, to, _, _ in channels.meta.sent)
    assert channels.meta.sent[1][2].startswith("Sentence 1 of 3:")
    assert channels.meta.sent[2][2].startswith("✅ Received. Sentence 2 of 3:")
    assert channels.twilio.sent == []


def test_twilio_conversation_is_answered_only_via_twilio(
    client: TestClient, channels: Channels, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "whatsapp_provider", "meta")  # the default doesn't matter
    from_twilio(client, Body="Hi")
    from_twilio(client, Body="1")
    from_twilio(client, NumMedia="1")

    rows = outbound_rows()
    assert len(rows) == 3 and all(r.channel == "twilio" and r.status == "sent" for r in rows)
    assert [r.provider_message_id for r in rows] == [sent[0] for sent in channels.twilio.sent]
    assert channels.meta.sent == []


def test_switching_channel_mid_session_moves_the_next_prompt(client: TestClient, channels: Channels) -> None:
    from_twilio(client, Body="Hi")
    from_twilio(client, Body="1")  # sentence 1 goes out via Twilio
    assert len(channels.twilio.sent) == 2 and channels.meta.sent == []

    from_meta(client, mp.voice_note(WA_ID))  # the answer arrives via Meta

    first, second = assignments()[:2]
    assert first.status == "recorded"
    assert len(channels.twilio.sent) == 2
    ((meta_id, _, body, _),) = channels.meta.sent
    assert body.startswith("✅ Received. Sentence 2 of 3:")
    assert second.outbound_message_sid == meta_id
    assert outbound_rows()[-1].channel == "meta"


# ===========
# SWIPE-REPLY
# ===========
def test_meta_swipe_reply_binds_to_the_replied_prompt(
    client: TestClient, channels: Channels, other_opus_bytes: bytes
) -> None:
    from_meta(client, mp.text(WA_ID, "Hi"))
    from_meta(client, mp.text(WA_ID, "1"))
    from_meta(client, mp.voice_note(WA_ID))  # answers sentence 1; sentence 2 goes out
    first, second = assignments()[:2]

    # A re-recording of sentence 1 (different audio, so not a duplicate), swiped onto its prompt.
    channels.meta.media = other_opus_bytes
    from_meta(client, mp.voice_note(WA_ID, **mp.reply_context(first.outbound_message_sid)))
    assert recordings()[-1].assignment_id == first.id


def test_swipe_reply_never_binds_across_channels(
    client: TestClient, channels: Channels, other_opus_bytes: bytes
) -> None:
    from_meta(client, mp.text(WA_ID, "Hi"))
    from_meta(client, mp.text(WA_ID, "1"))
    from_meta(client, mp.voice_note(WA_ID))  # sentence 2 is now the open prompt
    first, second = assignments()[:2]

    # Via Twilio, "replying" to the Meta ID of sentence 1: no binding, so the
    # answer goes to the open prompt (sentence 2), as without a reply.
    channels.twilio.media = other_opus_bytes
    from_twilio(client, NumMedia="1", OriginalRepliedMessageSid=first.outbound_message_sid)
    assert recordings()[-1].assignment_id == second.id


# ============================
# DELIVERY FAILURES AFTER SEND
# ============================
def conversation_state() -> str:
    from src.storage.models import ConversationState

    with db.get_sync_sessionmaker()() as session:
        return session.get(ConversationState, "P0001").state


def test_failed_delivery_status_rolls_the_conversation_back(
    client: TestClient, channels: Channels, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.conversation.outbound import restore_undelivered

    restores: list[int] = []
    monkeypatch.setattr("src.api.inbound.enqueue_restore", lambda outbound_id: restores.append(outbound_id))
    from_meta(client, mp.text(WA_ID, "Hi"))
    assert conversation_state() == "awaiting_ready"
    ((welcome_id, _, _, _),) = channels.meta.sent

    _post_statuses(client, mp.failed_status(welcome_id, WA_ID, code=131026))
    (outbound_id,) = restores
    restore_undelivered(outbound_id)  # what the queued job runs

    assert conversation_state() == "idle"
    (row,) = outbound_rows()
    assert row.status == "failed" and row.error_details["code"] == 131026

    # Their next message gets the welcome again, not the first sentence.
    from_meta(client, mp.text(WA_ID, "1"))
    last = channels.meta.sent[-1][2]
    assert "Are you ready to start?" in last and "Sentence 1 of 3" not in last


def test_delivered_reply_is_never_rolled_back(client: TestClient, channels: Channels) -> None:
    from src.conversation.outbound import restore_undelivered

    from_meta(client, mp.text(WA_ID, "Hi"))
    ((welcome_id, _, _, _),) = channels.meta.sent
    _post_statuses(client, mp.status(welcome_id, "delivered", WA_ID))
    (row,) = outbound_rows()
    restore_undelivered(row.id)  # e.g. a stray job: nothing to undo
    assert conversation_state() == "awaiting_ready"
