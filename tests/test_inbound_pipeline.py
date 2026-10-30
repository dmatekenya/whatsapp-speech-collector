import fakeredis
from fastapi.testclient import TestClient
from rq import Queue
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from src.api.main import EMPTY_TWIML, app
from src.config import get_settings
from src.storage import db
from src.storage.models import InboundMessage
from src.storage.queue import INBOUND_QUEUE, PROCESS_INBOUND_JOB, get_inbound_queue
from src.workers.jobs import process_inbound
from tests.conftest import count_rows

WEBHOOK_PATH = "/whatsapp/webhook"

VOICE_NOTE = {
    "MessageSid": "MM00000000000000000000000000000001",
    "From": "whatsapp:+265991234567",
    "To": "whatsapp:+14155238886",
    "Body": "",
    "NumMedia": "1",
    "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM1/Media/ME1",
    "MediaContentType0": "audio/ogg",
}


# ============================================================
# Helpers
# ============================================================
def post_signed(client: TestClient, params: dict[str, str], signature: str | None = None):
    settings = get_settings()
    if signature is None:
        url = settings.public_base_url + WEBHOOK_PATH
        signature = RequestValidator(settings.twilio_auth_token).compute_signature(url, params)
    return client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": signature})


def load_message(message_sid: str) -> InboundMessage:
    with db.get_sync_sessionmaker()() as session:
        return session.scalars(
            select(InboundMessage).where(InboundMessage.message_sid == message_sid)
        ).one()


# ============================================================
# Webhook
# ============================================================
def test_valid_webhook_stores_and_enqueues(client: TestClient, fake_queue: Queue) -> None:
    response = post_signed(client, VOICE_NOTE)

    assert response.status_code == 200
    assert response.text == EMPTY_TWIML
    assert response.headers["content-type"].startswith("application/xml")

    assert count_rows() == 1
    message = load_message(VOICE_NOTE["MessageSid"])
    assert message.status == "queued"
    assert message.sender == "+265991234567"
    assert message.message_type == "audio"
    assert message.channel == "twilio"
    assert message.raw_payload == VOICE_NOTE

    assert fake_queue.count == 1
    job = fake_queue.jobs[0]
    assert job.func_name == PROCESS_INBOUND_JOB
    assert job.args == (VOICE_NOTE["MessageSid"],)
    assert job.retries_left == 3


def test_duplicate_message_sid_stored_and_enqueued_once(
    client: TestClient, fake_queue: Queue
) -> None:
    first = post_signed(client, VOICE_NOTE)
    second = post_signed(client, VOICE_NOTE)

    assert first.status_code == second.status_code == 200
    assert second.text == EMPTY_TWIML
    assert count_rows() == 1
    assert fake_queue.count == 1


def test_invalid_signature_rejected_and_not_stored(client: TestClient, fake_queue: Queue) -> None:
    response = post_signed(client, VOICE_NOTE, signature="not-a-valid-signature")

    assert response.status_code == 403
    assert count_rows() == 0
    assert fake_queue.count == 0


def test_enqueue_failure_keeps_message_received(client: TestClient) -> None:
    server = fakeredis.FakeServer()
    server.connected = False  # Simulate Redis being down.
    app.dependency_overrides[get_inbound_queue] = lambda: Queue(
        INBOUND_QUEUE, connection=fakeredis.FakeRedis(server=server)
    )

    response = post_signed(client, VOICE_NOTE)

    assert response.status_code == 200
    assert response.text == EMPTY_TWIML
    assert load_message(VOICE_NOTE["MessageSid"]).status == "received"


# ============================================================
# Worker
# ============================================================
def test_process_inbound_marks_processed_and_is_idempotent(
    client: TestClient, fake_channel: object
) -> None:
    post_signed(client, VOICE_NOTE)
    sid = VOICE_NOTE["MessageSid"]

    process_inbound(sid)
    first = load_message(sid)
    assert first.status == "processed"
    assert first.processed_at is not None
    assert first.attempts == 1
    assert first.error is None

    process_inbound(sid)
    second = load_message(sid)
    assert second.status == "processed"
    assert second.attempts == 1
    assert second.processed_at == first.processed_at
