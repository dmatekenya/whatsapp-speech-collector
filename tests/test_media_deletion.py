import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import select
from twilio.http import HttpClient
from twilio.http.response import Response
from twilio.rest import Client

from scripts.delete_channel_media import delete_pending_media
from scripts.process_audio_backlog import process_audio_backlog
from src.channels.base import MediaDeleteError
from src.channels.twilio import TwilioChannel
from src.config import get_settings
from src.storage import db
from src.storage.models import InboundMessage, Recording
from src.workers.jobs import DELETE_MEDIA_JOB, delete_channel_media, process_inbound
from tests.conftest import REGISTERED_SENDER, FakeChannel

UNREGISTERED = "+265881234567"
ACCOUNT = "AC" + "0" * 32
TWILIO_REFUSAL = MediaDeleteError(
    "Twilio refused to delete media for MM1: HTTP 400, error 20009: Cannot delete media yet",
    status=400,
    code=20009,
)


# =======
# HELPERS
# =======
def payload(sid: str, sender: str, media: int = 1) -> dict[str, str]:
    data = {"MessageSid": sid, "From": f"whatsapp:{sender}", "Body": "", "NumMedia": str(media)}
    for n in range(media):
        data[f"MediaUrl{n}"] = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{sid}/Media/ME{n:032d}"
        data[f"MediaContentType{n}"] = "audio/ogg"
    return data


def add_message(sid: str, sender: str, media: int = 1, message_type: str = "audio") -> None:
    with db.get_sync_sessionmaker()() as session:
        session.add(
            InboundMessage(
                message_sid=sid,
                sender=sender,
                message_type=message_type,
                raw_payload=payload(sid, sender, media),
                status="queued",
            )
        )
        session.commit()


def load(sid: str) -> tuple[InboundMessage, Recording | None]:
    with db.get_sync_sessionmaker()() as session:
        message = session.scalars(select(InboundMessage).where(InboundMessage.message_sid == sid)).one()
        recording = session.scalar(select(Recording).where(Recording.inbound_message_id == message.id))
        return message, recording


@pytest.fixture
def deletion_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "twilio_delete_media", True)


class FakeTwilioHttp(HttpClient):
    """Records Twilio API requests and replies with canned responses."""

    def __init__(self, responses: list[Response]) -> None:
        super().__init__(logging.getLogger("fake-twilio"), False)
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, params=None, data=None, headers=None, auth=None, timeout=None, allow_redirects=False):  # noqa: ANN001, ANN201, PLR0913
        self.calls.append((method, url))
        return self.responses.pop(0)


def twilio_channel(http: FakeTwilioHttp) -> TwilioChannel:
    channel = TwilioChannel(ACCOUNT, "token", "+14155238886")
    channel._client = Client(ACCOUNT, "token", http_client=http)
    return channel


# ==============
# TWILIO ADAPTER
# ==============
def test_delete_media_calls_the_media_endpoint_for_every_item() -> None:
    http = FakeTwilioHttp([Response(204, ""), Response(204, "")])
    channel = twilio_channel(http)

    channel.delete_media(channel.parse_inbound(payload("MM" + "1" * 32, UNREGISTERED, media=2)))

    assert [method for method, _ in http.calls] == ["DELETE", "DELETE"]
    assert [url.split("/")[-1] for _, url in http.calls] == [f"ME{0:032d}.json", f"ME{1:032d}.json"]
    assert all(f"/Messages/MM{'1' * 32}/Media/" in url for _, url in http.calls)


def test_already_deleted_media_counts_as_deleted() -> None:
    http = FakeTwilioHttp([Response(404, json.dumps({"code": 20404, "message": "Not found"}))])
    channel = twilio_channel(http)

    channel.delete_media(channel.parse_inbound(payload("MM" + "2" * 32, UNREGISTERED)))


def test_twilio_refusal_raises_with_code_and_message_but_no_urls() -> None:
    body = {
        "code": 20009,
        "message": f"Cannot delete /2010-04-01/Accounts/{ACCOUNT}/Messages/MM{'3' * 32}/Media/ME{'0' * 32}.json"
        " while it is being processed. See https://www.twilio.com/docs/errors/20009",
        "status": 400,
    }
    http = FakeTwilioHttp([Response(400, json.dumps(body))])
    channel = twilio_channel(http)

    with pytest.raises(MediaDeleteError) as error:
        channel.delete_media(channel.parse_inbound(payload("MM" + "3" * 32, UNREGISTERED)))

    text = str(error.value)
    assert error.value.status == 400 and error.value.code == 20009
    assert "error 20009" in text and "while it is being processed" in text
    assert "/2010-04-01/" not in text and "https://" not in text and ACCOUNT not in text


# =================
# STORED RECORDINGS
# =================
def test_flag_set_when_deletion_succeeds(fake_channel: FakeChannel, deletion_on: None) -> None:
    add_message("MM_OK", REGISTERED_SENDER)

    process_inbound("MM_OK")

    message, recording = load("MM_OK")
    assert recording.status == "stored" and recording.twilio_media_deleted
    assert message.media_deleted_at is not None
    assert fake_channel.delete_calls == [("MM_OK", "stored")]
    assert fake_channel.queue.count == 0  # no retry needed


def test_flag_stays_false_and_warning_logged_when_deletion_fails(
    fake_channel: FakeChannel, deletion_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    fake_channel.delete_error = TWILIO_REFUSAL
    add_message("MM_REFUSED", REGISTERED_SENDER)

    with caplog.at_level(logging.WARNING, logger="src.workers.jobs"):
        process_inbound("MM_REFUSED")

    message, recording = load("MM_REFUSED")
    assert message.status == "processed"  # the job itself succeeded: the audio is safe
    assert recording.status == "stored" and not recording.twilio_media_deleted
    assert message.media_deleted_at is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        f"Could not delete channel media for recording id={recording.id}: "
        "Twilio refused to delete media for MM1: HTTP 400, error 20009: Cannot delete media yet"
    ]
    # A retry job is scheduled, with backoff.
    (job,) = fake_channel.queue.jobs
    assert job.func_name == DELETE_MEDIA_JOB and job.args == ("MM_REFUSED",)
    assert job.retries_left == 5

    # The retry fails while Twilio keeps refusing (so RQ retries again) ...
    with pytest.raises(MediaDeleteError):
        delete_channel_media("MM_REFUSED")
    # ... and records the deletion once Twilio accepts.
    fake_channel.delete_error = None
    delete_channel_media("MM_REFUSED")
    message, recording = load("MM_REFUSED")
    assert recording.twilio_media_deleted and message.media_deleted_at is not None


def test_nothing_deleted_when_setting_off(
    fake_channel: FakeChannel, caplog: pytest.LogCaptureFixture
) -> None:
    assert get_settings().twilio_delete_media is False
    add_message("MM_OFF", REGISTERED_SENDER)

    with caplog.at_level(logging.INFO, logger="src.workers.jobs"):
        process_inbound("MM_OFF")

    assert fake_channel.delete_calls == []
    assert any("Channel media deletion is off" in r.getMessage() for r in caplog.records)


def test_retry_job_never_deletes_media_that_is_not_stored(
    fake_channel: FakeChannel, deletion_on: None
) -> None:
    add_message("MM_PENDING", REGISTERED_SENDER)  # registered, but not processed yet

    delete_channel_media("MM_PENDING")

    assert fake_channel.delete_calls == []
    assert load("MM_PENDING")[0].media_deleted_at is None


# ====================
# UNREGISTERED SENDERS
# ====================
def test_unregistered_media_deleted_without_download(fake_channel: FakeChannel, deletion_on: None) -> None:
    add_message("MM_UNREG", UNREGISTERED)

    process_inbound("MM_UNREG")

    message, recording = load("MM_UNREG")
    assert message.status == "unregistered" and recording is None
    assert fake_channel.download_calls == []
    assert fake_channel.delete_calls == [("MM_UNREG", None)]
    assert message.media_deleted_at is not None


def test_unregistered_media_kept_when_setting_off(fake_channel: FakeChannel) -> None:
    add_message("MM_UNREG_OFF", UNREGISTERED)

    process_inbound("MM_UNREG_OFF")

    assert fake_channel.delete_calls == [] and load("MM_UNREG_OFF")[0].media_deleted_at is None


def test_unregistered_text_has_nothing_to_delete(fake_channel: FakeChannel, deletion_on: None) -> None:
    add_message("SM_UNREG", UNREGISTERED, media=0, message_type="text")

    process_inbound("SM_UNREG")

    assert fake_channel.delete_calls == []


def test_unregistered_refusal_schedules_retry(fake_channel: FakeChannel, deletion_on: None) -> None:
    fake_channel.delete_error = TWILIO_REFUSAL
    add_message("MM_UNREG_RETRY", UNREGISTERED)

    process_inbound("MM_UNREG_RETRY")

    assert load("MM_UNREG_RETRY")[0].media_deleted_at is None
    assert [job.args for job in fake_channel.queue.jobs] == [("MM_UNREG_RETRY",)]
    fake_channel.delete_error = None
    delete_channel_media("MM_UNREG_RETRY")
    assert load("MM_UNREG_RETRY")[0].media_deleted_at is not None


def test_deleted_media_is_not_downloaded_later(fake_channel: FakeChannel, deletion_on: None) -> None:
    add_message("MM_LATER", UNREGISTERED)
    process_inbound("MM_LATER")  # media deleted
    from tests.conftest import add_participant

    add_participant("P0002", UNREGISTERED)  # registers afterwards

    assert process_audio_backlog(dry_run=True) == 0  # the backlog skips it
    process_inbound("MM_LATER")  # a direct re-run doesn't try to download either

    message, recording = load("MM_LATER")
    assert fake_channel.download_calls == []
    assert message.status == "failed" and "deleted before it was stored" in message.error
    assert recording.status == "pending"


# ===============
# ONE-OFF CLEANUP
# ===============
def test_cleanup_script_dry_run_then_delete(fake_channel: FakeChannel, audio_root: Path) -> None:
    # Two stored recordings and one unregistered voice note, all with media
    # still on Twilio (deletion was off when they arrived).
    add_message("MM_A", REGISTERED_SENDER)
    add_message("MM_B", REGISTERED_SENDER)
    add_message("MM_C", UNREGISTERED)
    add_message("SM_D", UNREGISTERED, media=0, message_type="text")
    for sid in ("MM_A", "MM_B", "MM_C", "SM_D"):
        process_inbound(sid)
    factory = db.get_sync_sessionmaker()

    dry = delete_pending_media(factory, fake_channel, dry_run=True)
    assert sorted((c.message_sid, c.kind) for c in dry.candidates) == [
        ("MM_A", "stored recording"),
        ("MM_B", "stored recording"),
        ("MM_C", "unregistered sender"),
    ]
    assert fake_channel.delete_calls == []

    original = fake_channel.delete_media

    def fail_on_b(message):  # noqa: ANN001, ANN202
        if message.message_id == "MM_B":
            raise TWILIO_REFUSAL
        return original(message)

    fake_channel.delete_media = fail_on_b
    result = delete_pending_media(factory, fake_channel)

    assert sorted(c.message_sid for c in result.deleted) == ["MM_A", "MM_C"]
    assert [(c.message_sid, error) for c, error in result.failed] == [("MM_B", str(TWILIO_REFUSAL))]
    a, rec_a = load("MM_A")
    b, rec_b = load("MM_B")
    c, _ = load("MM_C")
    assert rec_a.twilio_media_deleted and a.media_deleted_at is not None
    assert not rec_b.twilio_media_deleted and b.media_deleted_at is None
    assert c.media_deleted_at is not None

    # Re-running only picks up what's left.
    fake_channel.delete_media = original
    again = delete_pending_media(factory, fake_channel)
    assert [c.message_sid for c in again.deleted] == ["MM_B"] and again.failed == []
    assert delete_pending_media(factory, fake_channel, dry_run=True).candidates == []
