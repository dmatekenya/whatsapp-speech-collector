from pathlib import Path

from sqlalchemy import select

from scripts.relocate_recordings import relocate_recordings
from src.storage import db
from src.storage.audio import get_audio_storage
from src.storage.models import InboundMessage, Recording
from src.workers.jobs import process_inbound
from tests.conftest import FakeChannel, add_participant

UNREGISTERED = "+265881234567"


# ============================================================
# Helpers
# ============================================================
def add_message(sid: str, sender: str, message_type: str = "audio", status: str = "queued") -> int:
    payload = {
        "MessageSid": sid,
        "From": f"whatsapp:{sender}",
        "Body": "" if message_type == "audio" else "Moni",
        "MediaUrl0": f"https://api.twilio.com/Messages/{sid}/Media/ME1",
        "MediaContentType0": "audio/ogg" if message_type == "audio" else "",
    }
    with db.get_sync_sessionmaker()() as session:
        message = InboundMessage(
            message_sid=sid,
            sender=sender,
            message_type=message_type,
            raw_payload=payload,
            status=status,
        )
        session.add(message)
        session.commit()
        return message.id


def load_message(sid: str) -> InboundMessage:
    with db.get_sync_sessionmaker()() as session:
        return session.scalars(select(InboundMessage).where(InboundMessage.message_sid == sid)).one()


def recordings() -> list[Recording]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Recording).order_by(Recording.id)))


def add_unassigned_recording(message_id: int, data: bytes) -> Recording:
    """A recording stored before the registry existed (step 3 layout)."""
    storage = get_audio_storage()
    with db.get_sync_sessionmaker()() as session:
        recording = Recording(inbound_message_id=message_id, status="stored")
        session.add(recording)
        session.flush()
        recording.folder = "_unassigned"
        recording.original_path = f"_unassigned/UNASSIGNED_R{recording.id:06d}.ogg"
        recording.wav_path = f"_unassigned/UNASSIGNED_R{recording.id:06d}.wav"
        storage.save(recording.original_path, data)
        storage.save(recording.wav_path, b"RIFF fake wav")
        session.commit()
        return recording


def files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# ============================================================
# Worker
# ============================================================
def test_unregistered_sender_is_not_processed(
    fake_channel: FakeChannel, audio_root: Path
) -> None:
    add_message("MM_UNREG_AUDIO", UNREGISTERED)
    add_message("SM_UNREG_TEXT", UNREGISTERED, message_type="text")

    process_inbound("MM_UNREG_AUDIO")
    process_inbound("SM_UNREG_TEXT")

    for sid in ("MM_UNREG_AUDIO", "SM_UNREG_TEXT"):
        message = load_message(sid)
        assert message.status == "unregistered"
        assert message.participant_id is None
    assert fake_channel.download_calls == []
    assert recordings() == []
    assert files_under(audio_root) == []


def test_inactive_participant_is_not_processed(fake_channel: FakeChannel) -> None:
    add_participant("P0002", UNREGISTERED, status="withdrawn")
    add_message("MM_WITHDRAWN", UNREGISTERED)

    process_inbound("MM_WITHDRAWN")

    assert load_message("MM_WITHDRAWN").status == "unregistered"
    assert fake_channel.download_calls == []


def test_sender_registered_later_is_processed_on_rerun(fake_channel: FakeChannel) -> None:
    add_message("MM_LATER", UNREGISTERED)
    process_inbound("MM_LATER")
    assert load_message("MM_LATER").status == "unregistered"

    add_participant("P0002", UNREGISTERED)
    process_inbound("MM_LATER")

    message = load_message("MM_LATER")
    assert message.status == "processed" and message.participant_id == "P0002"
    (recording,) = recordings()
    assert recording.participant_id == "P0002"
    assert recording.wav_path == f"P0002/P0002_R{recording.id:06d}.wav"


# ============================================================
# Relocation
# ============================================================
def test_relocation_moves_and_renames_registered_only(
    clean_db: None, audio_root: Path, opus_bytes: bytes
) -> None:
    later = "+265887654321"
    moved = add_unassigned_recording(add_message("MM_OLD_1", later, status="processed"), opus_bytes)
    stays = add_unassigned_recording(
        add_message("MM_OLD_2", UNREGISTERED, status="processed"), opus_bytes
    )
    add_participant("P0007", later)
    before = files_under(audio_root)

    dry = relocate_recordings(dry_run=True)
    assert (dry.moved, dry.unregistered) == (1, 1)
    assert files_under(audio_root) == before

    result = relocate_recordings()

    assert (result.moved, result.unregistered, result.failed) == (1, 1, 0)
    new_ogg = f"P0007/P0007_R{moved.id:06d}.ogg"
    new_wav = f"P0007/P0007_R{moved.id:06d}.wav"
    assert files_under(audio_root) == sorted(
        [new_ogg, new_wav, stays.original_path, stays.wav_path]
    )
    assert (audio_root / new_ogg).read_bytes() == opus_bytes

    relocated, untouched = recordings()
    assert relocated.participant_id == "P0007"
    assert relocated.folder == "P0007"
    assert (relocated.original_path, relocated.wav_path) == (new_ogg, new_wav)
    assert load_message("MM_OLD_1").participant_id == "P0007"
    assert untouched.participant_id is None
    assert untouched.wav_path == stays.wav_path

    again = relocate_recordings()
    assert (again.moved, again.unregistered) == (0, 1)


def test_relocation_rolls_back_files_if_target_exists(
    clean_db: None, audio_root: Path, opus_bytes: bytes
) -> None:
    later = "+265887654321"
    recording = add_unassigned_recording(
        add_message("MM_OLD_3", later, status="processed"), opus_bytes
    )
    add_participant("P0007", later)
    # A file already sits where the WAV would go: the move must not overwrite it.
    get_audio_storage().save(f"P0007/P0007_R{recording.id:06d}.wav", b"existing")

    result = relocate_recordings()

    assert result.failed == 1
    (unchanged,) = recordings()
    assert unchanged.participant_id is None
    assert (audio_root / unchanged.original_path).read_bytes() == opus_bytes
    assert (audio_root / unchanged.wav_path).is_file()
    assert (audio_root / f"P0007/P0007_R{recording.id:06d}.wav").read_bytes() == b"existing"
