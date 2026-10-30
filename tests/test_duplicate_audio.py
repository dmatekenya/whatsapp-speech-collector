import hashlib
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update

from scripts.process_audio_backlog import backfill_hashes
from src.storage import db
from src.storage.models import InboundMessage, Recording
from src.workers.jobs import process_inbound
from tests.conftest import FakeChannel

SENDER = "+265991234567"


# ============================================================
# Helpers
# ============================================================
def voice_note(n: int) -> dict[str, str]:
    return {
        "MessageSid": f"MM{n:032d}",
        "From": f"whatsapp:{SENDER}",
        "To": "whatsapp:+14155238886",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": f"https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM{n}/Media/ME{n}",
        "MediaContentType0": "audio/ogg",
    }


def receive(payload: dict[str, str], received_at: datetime | None = None) -> int:
    """Store an inbound message without processing it; return its id."""
    with db.get_sync_sessionmaker()() as session:
        message = InboundMessage(
            message_sid=payload["MessageSid"],
            sender=SENDER,
            message_type="audio",
            raw_payload=payload,
            status="queued",
        )
        if received_at is not None:
            message.received_at = received_at
        session.add(message)
        session.commit()
        return message.id


def receive_and_process(payload: dict[str, str]) -> None:
    receive(payload)
    process_inbound(payload["MessageSid"])


def recording_for(message_id: int) -> Recording:
    with db.get_sync_sessionmaker()() as session:
        return session.scalars(
            select(Recording).where(Recording.inbound_message_id == message_id)
        ).one()


def recordings() -> list[Recording]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Recording).order_by(Recording.id)))


def links() -> list[tuple[int, int | None]]:
    return [(r.id, r.duplicate_of_recording_id) for r in recordings()]


# ============================================================
# Worker
# ============================================================
def test_identical_audio_is_stored_and_flagged(
    clean_db: None, fake_channel: FakeChannel, audio_root: Path, opus_bytes: bytes
) -> None:
    receive_and_process(voice_note(1))
    receive_and_process(voice_note(2))
    receive_and_process(voice_note(3))

    first, second, third = recordings()
    expected_hash = hashlib.sha256(opus_bytes).hexdigest()
    assert all(r.status == "stored" for r in (first, second, third))
    assert {r.content_sha256 for r in (first, second, third)} == {expected_hash}
    assert first.duplicate_of_recording_id is None
    # Every copy points at the earliest recording, not the previous copy.
    assert second.duplicate_of_recording_id == first.id
    assert third.duplicate_of_recording_id == first.id
    # Duplicates are still stored on disk.
    for r in (first, second, third):
        assert (audio_root / r.original_path).is_file()
        assert (audio_root / r.wav_path).is_file()


def test_hash_is_of_original_not_wav(
    clean_db: None, fake_channel: FakeChannel, audio_root: Path
) -> None:
    receive_and_process(voice_note(1))

    (recording,) = recordings()
    assert recording.content_sha256 == hashlib.sha256(
        (audio_root / recording.original_path).read_bytes()
    ).hexdigest()
    assert recording.content_sha256 != hashlib.sha256(
        (audio_root / recording.wav_path).read_bytes()
    ).hexdigest()


def test_different_audio_is_not_flagged(
    clean_db: None, fake_channel: FakeChannel, other_opus_bytes: bytes
) -> None:
    receive_and_process(voice_note(1))
    fake_channel.media = other_opus_bytes
    receive_and_process(voice_note(2))

    first, second = recordings()
    assert first.content_sha256 != second.content_sha256
    assert links() == [(first.id, None), (second.id, None)]


def test_original_is_earliest_received_not_lowest_recording_id(
    clean_db: None, fake_channel: FakeChannel
) -> None:
    # The first message is only processed later (e.g. by the backlog), so the
    # later message gets the lower recording id.
    earlier = receive(voice_note(1), received_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    later = receive(voice_note(2), received_at=datetime(2026, 10, 1, 9, 5, tzinfo=UTC))

    process_inbound(voice_note(2)["MessageSid"])
    process_inbound(voice_note(1)["MessageSid"])

    original, copy = recording_for(earlier), recording_for(later)
    assert copy.id < original.id
    assert original.duplicate_of_recording_id is None
    assert copy.duplicate_of_recording_id == original.id


def test_same_received_at_falls_back_to_message_id(
    clean_db: None, fake_channel: FakeChannel
) -> None:
    same_time = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    first = receive(voice_note(1), received_at=same_time)
    second = receive(voice_note(2), received_at=same_time)

    process_inbound(voice_note(2)["MessageSid"])
    process_inbound(voice_note(1)["MessageSid"])

    assert recording_for(first).duplicate_of_recording_id is None
    assert recording_for(second).duplicate_of_recording_id == recording_for(first).id


# ============================================================
# Backfill
# ============================================================
def test_backfill_restores_hashes_and_links_and_is_idempotent(
    clean_db: None, fake_channel: FakeChannel, other_opus_bytes: bytes, opus_bytes: bytes
) -> None:
    receive_and_process(voice_note(1))
    fake_channel.media = other_opus_bytes
    receive_and_process(voice_note(2))
    fake_channel.media = opus_bytes
    receive_and_process(voice_note(3))
    expected_hashes = [r.content_sha256 for r in recordings()]
    expected_links = links()
    first_id = expected_links[0][0]
    assert expected_links[2][1] == first_id

    # Simulate recordings stored before hashing existed.
    with db.get_sync_sessionmaker()() as session:
        session.execute(update(Recording).values(content_sha256=None, duplicate_of_recording_id=None))
        session.commit()

    dry = backfill_hashes(dry_run=True)
    assert (dry.hashed, dry.links_changed) == (3, 1)
    assert all(r.content_sha256 is None for r in recordings())

    first_run = backfill_hashes()
    assert (first_run.hashed, first_run.missing_files, first_run.links_changed) == (3, 0, 1)
    assert [r.content_sha256 for r in recordings()] == expected_hashes
    assert links() == expected_links

    second_run = backfill_hashes()
    assert (second_run.hashed, second_run.missing_files, second_run.links_changed) == (0, 0, 0)
    assert links() == expected_links


def test_backfill_corrects_links_set_by_recording_id(
    clean_db: None, fake_channel: FakeChannel
) -> None:
    earlier = receive(voice_note(1), received_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    later = receive(voice_note(2), received_at=datetime(2026, 10, 1, 9, 5, tzinfo=UTC))
    process_inbound(voice_note(2)["MessageSid"])
    process_inbound(voice_note(1)["MessageSid"])
    original, copy = recording_for(earlier), recording_for(later)

    # The old rule (lowest recording id wins) linked them the wrong way round,
    # like recordings 1 and 3 in the dev database.
    with db.get_sync_sessionmaker()() as session:
        session.execute(
            update(Recording).where(Recording.id == original.id).values(duplicate_of_recording_id=copy.id)
        )
        session.execute(
            update(Recording).where(Recording.id == copy.id).values(duplicate_of_recording_id=None)
        )
        session.commit()

    result = backfill_hashes()

    assert (result.hashed, result.links_changed) == (0, 2)
    assert recording_for(earlier).duplicate_of_recording_id is None
    assert recording_for(later).duplicate_of_recording_id == original.id
    assert backfill_hashes().links_changed == 0


def test_backfill_skips_missing_files(
    clean_db: None, fake_channel: FakeChannel, audio_root: Path
) -> None:
    receive_and_process(voice_note(1))
    (recording,) = recordings()
    (audio_root / recording.original_path).unlink()
    with db.get_sync_sessionmaker()() as session:
        session.execute(update(Recording).values(content_sha256=None))
        session.commit()

    result = backfill_hashes()

    assert (result.hashed, result.missing_files) == (0, 1)
    assert recordings()[0].content_sha256 is None
