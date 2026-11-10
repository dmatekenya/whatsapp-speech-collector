import os
import tempfile

# Point everything at the test database and a known Twilio token before any
# Settings are loaded. Postgres credentials still come from .env.
os.environ["POSTGRES_DB"] = "speech_collector_test"
os.environ["PUBLIC_BASE_URL"] = "https://test.example"
os.environ["TWILIO_ACCOUNT_SID"] = "ACtest"
os.environ["TWILIO_AUTH_TOKEN"] = "test-auth-token"
os.environ["TWILIO_WHATSAPP_NUMBER"] = "+14155238886"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"
os.environ["TWILIO_DELETE_MEDIA"] = "false"
os.environ["MEDIA_SIGNING_SECRET"] = "test-media-signing-secret"
# Fake Meta values, so a real META_* in .env never reaches the tests.
os.environ["WHATSAPP_PROVIDER"] = "twilio"
os.environ["META_VERIFY_TOKEN"] = "test-verify-token"
os.environ["META_APP_SECRET"] = "test-app-secret"
os.environ["META_ACCESS_TOKEN"] = "test-meta-access-token"
os.environ["META_PHONE_NUMBER_ID"] = "100000000000001"
os.environ["META_WABA_ID"] = "200000000000002"
os.environ["META_GRAPH_VERSION"] = "v23.0"
# Nothing listens here: a test that reaches for the real Redis fails loudly
# instead of enqueuing jobs that the real worker would run.
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
# Never write test audio under the real data/ folder.
os.environ["AUDIO_ROOT"] = tempfile.mkdtemp(prefix="speech-collector-test-audio-")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import AsyncIterator, Iterator  # noqa: E402
from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from rq import Queue  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from src.api.main import app, get_channel  # noqa: E402
from src.channels.base import InboundMessage as ChannelMessage  # noqa: E402
from src.channels.twilio import TwilioChannel  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.storage import db  # noqa: E402
from src.storage.audio import get_audio_storage  # noqa: E402
from src.storage.models import Participant, Recording  # noqa: E402

# The sender used by the worker tests; registered as P0001 by fake_channel.
REGISTERED_SENDER = "+265991234567"
from src.storage.queue import INBOUND_QUEUE, get_inbound_queue  # noqa: E402


# ========
# DATABASE
# ========
@pytest.fixture(scope="session")
def test_database() -> Iterator[None]:
    """Migrate the test database, or skip if Postgres isn't reachable."""
    for cached in (get_settings, get_channel, db.get_sync_engine, db.get_sync_sessionmaker):
        cached.cache_clear()
    settings = get_settings()
    assert settings.postgres_db == "speech_collector_test"

    try:
        with db.get_sync_engine().connect():
            pass
    except OperationalError as exc:
        pytest.skip(
            f"Postgres test database {settings.postgres_db!r} not reachable at "
            f"{settings.postgres_host}:{settings.postgres_port}, skipping DB tests "
            f"({type(exc.orig).__name__})"
        )

    command.upgrade(Config("alembic.ini"), "head")
    yield
    db.get_sync_engine().dispose()


@pytest.fixture
def clean_db(test_database: None) -> Iterator[None]:
    with db.get_sync_engine().begin() as connection:
        connection.execute(
            text(
                "TRUNCATE qc_results, transcripts, asr_batches, outbound_messages, conversation_state, assignments,"
                " sessions, recordings, inbound_messages, participants, prompts RESTART IDENTITY"
            )
        )
        connection.execute(text("ALTER SEQUENCE participant_id_seq RESTART"))
        connection.execute(text("ALTER SEQUENCE prompt_sentence_id_seq RESTART"))
        connection.execute(text("ALTER SEQUENCE prompt_conversation_starter_id_seq RESTART"))
        connection.execute(text("ALTER SEQUENCE prompt_image_id_seq RESTART"))
    yield


def count_rows() -> int:
    with db.get_sync_engine().connect() as connection:
        return connection.scalar(text("SELECT count(*) FROM inbound_messages"))


# ====================
# QUEUE AND API CLIENT
# ====================
@pytest.fixture
def fake_queue() -> Queue:
    return Queue(INBOUND_QUEUE, connection=fakeredis.FakeRedis())


@pytest.fixture
def client(clean_db: None, fake_queue: Queue) -> Iterator[TestClient]:
    # TestClient runs the app on its own event loop; NullPool means no
    # asyncpg connection outlives the loop it was created on.
    engine = create_async_engine(get_settings().async_database_url, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[db.get_async_session] = override_session
    app.dependency_overrides[get_inbound_queue] = lambda: fake_queue
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ======================
# AUDIO AND FAKE CHANNEL
# ======================
def make_opus(path: Path, frequency: int) -> bytes:
    """A 2-second sine wave as Opus in OGG, like a WhatsApp voice note."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed, skipping audio tests")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=2",
            "-c:a", "libopus", "-b:a", "24k", str(path),
        ],
        check=True,
        timeout=60,
    )  # fmt: skip
    return path.read_bytes()


@pytest.fixture(scope="session")
def opus_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return make_opus(tmp_path_factory.mktemp("opus") / "sine-440.ogg", 440)


@pytest.fixture(scope="session")
def other_opus_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return make_opus(tmp_path_factory.mktemp("opus") / "sine-880.ogg", 880)


@pytest.fixture
def audio_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "audio"
    monkeypatch.setattr(get_settings(), "audio_root", root)
    get_audio_storage.cache_clear()
    yield root
    get_audio_storage.cache_clear()


class FakeChannel(TwilioChannel):
    """Parses like Twilio, but serves fixed bytes and records delete calls."""

    def __init__(self, media: bytes) -> None:
        super().__init__(account_sid="ACtest", auth_token="test-auth-token", whatsapp_number="+1")
        self.media = media
        self.download_error: Exception | None = None
        self.download_calls: list[str] = []
        # (message_id, recording status at the moment delete_media was called)
        self.delete_calls: list[tuple[str, str | None]] = []
        self.delete_error: Exception | None = None

    def download_media(self, message: ChannelMessage) -> bytes:
        self.download_calls.append(message.message_id)
        if self.download_error is not None:
            raise self.download_error
        return self.media

    def delete_media(self, message: ChannelMessage) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        with db.get_sync_sessionmaker()() as session:
            status = session.scalar(select(Recording.status))
        self.delete_calls.append((message.message_id, status))

    def send_text(self, to: str, body: str) -> str:
        raise AssertionError("tests must not send messages")


def add_participant(
    participant_id: str, phone_number: str, status: str = "active", name: str = "Test Person"
) -> None:
    with db.get_sync_sessionmaker()() as session:
        session.add(
            Participant(
                participant_id=participant_id,
                name=name,
                phone_number=phone_number,
                status=status,
                batch="test",
                enrolled_on=date(2026, 1, 1),
                source_file="test.csv",
            )
        )
        session.commit()


@pytest.fixture
def registered_sender(clean_db: None) -> str:
    add_participant("P0001", REGISTERED_SENDER)
    return "P0001"


@pytest.fixture
def fake_channel(
    opus_bytes: bytes, audio_root: Path, registered_sender: str, monkeypatch: pytest.MonkeyPatch
) -> FakeChannel:
    channel = FakeChannel(opus_bytes)
    monkeypatch.setattr("src.workers.jobs.get_channel", lambda name=None: channel)
    # Retry jobs go to fakeredis, never the real queue.
    channel.queue = Queue(INBOUND_QUEUE, connection=fakeredis.FakeRedis())
    monkeypatch.setattr("src.workers.jobs.get_inbound_queue", lambda: channel.queue)
    return channel
