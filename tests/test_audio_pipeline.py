import subprocess
import wave
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.audio.convert import AudioConversionError, probe
from src.channels.base import MediaDownloadError
from src.config import get_settings
from src.storage import db
from src.storage.audio import recording_filename, resolve_folder
from src.storage.models import InboundMessage, Recording
from src.workers.jobs import process_inbound
from tests.conftest import FakeChannel

SENDER = "+265991234567"

VOICE_NOTE = {
    "MessageSid": "MM00000000000000000000000000000010",
    "From": f"whatsapp:{SENDER}",
    "To": "whatsapp:+14155238886",
    "Body": "",
    "NumMedia": "1",
    "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM10/Media/ME10",
    "MediaContentType0": "audio/ogg",
}

TEXT = {
    "MessageSid": "SM00000000000000000000000000000011",
    "From": f"whatsapp:{SENDER}",
    "To": "whatsapp:+14155238886",
    "Body": "Moni",
    "NumMedia": "0",
}


# ============================================================
# Helpers
# ============================================================
def add_message(payload: dict[str, str], message_type: str) -> None:
    with db.get_sync_sessionmaker()() as session:
        session.add(
            InboundMessage(
                message_sid=payload["MessageSid"],
                sender=SENDER,
                message_type=message_type,
                raw_payload=payload,
                status="queued",
            )
        )
        session.commit()


def load(message_sid: str) -> tuple[InboundMessage, Recording | None]:
    with db.get_sync_sessionmaker()() as session:
        message = session.scalars(
            select(InboundMessage).where(InboundMessage.message_sid == message_sid)
        ).one()
        recording = session.scalar(
            select(Recording).where(Recording.inbound_message_id == message.id)
        )
        return message, recording


def recording_count() -> int:
    with db.get_sync_sessionmaker()() as session:
        return session.scalar(select(func.count()).select_from(Recording))


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ============================================================
# Naming
# ============================================================
def test_recording_filename_and_folder() -> None:
    assert recording_filename(None, 123, "wav") == "UNASSIGNED_R000123.wav"
    assert recording_filename("P0001", 123, ".ogg") == "P0001_R000123.ogg"
    assert resolve_folder(SENDER, None) == "_unassigned"
    assert resolve_folder(SENDER, "P0001") == "P0001"
    for unsafe in (SENDER, "265991234567", "../P1", "P 1"):
        with pytest.raises(ValueError):
            resolve_folder(SENDER, unsafe)


# ============================================================
# Worker
# ============================================================
def test_audio_message_stores_original_and_wav(
    clean_db: None, fake_channel: FakeChannel, audio_root: Path
) -> None:
    add_message(VOICE_NOTE, "audio")

    process_inbound(VOICE_NOTE["MessageSid"])

    message, recording = load(VOICE_NOTE["MessageSid"])
    assert message.status == "processed"
    assert recording is not None
    assert recording.status == "stored"
    assert message.participant_id == "P0001"
    assert recording.participant_id == "P0001"
    assert recording.folder == "P0001"
    assert recording.original_path == f"P0001/P0001_R{recording.id:06d}.ogg"
    assert recording.wav_path == f"P0001/P0001_R{recording.id:06d}.wav"
    assert recording.original_content_type == "audio/ogg"
    assert SENDER.lstrip("+") not in recording.wav_path

    original = audio_root / recording.original_path
    wav_file = audio_root / recording.wav_path
    assert original.read_bytes() == fake_channel.media
    assert files_under(audio_root) == sorted([original, wav_file])

    with wave.open(str(wav_file)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
    info = probe(wav_file)
    assert info.duration_s == pytest.approx(2.0, abs=0.1)
    assert recording.duration_s == pytest.approx(info.duration_s)
    assert (recording.sample_rate, recording.channels) == (16000, 1)
    assert recording.size_bytes == wav_file.stat().st_size


def test_second_run_creates_no_duplicates(
    clean_db: None, fake_channel: FakeChannel, audio_root: Path
) -> None:
    add_message(VOICE_NOTE, "audio")

    process_inbound(VOICE_NOTE["MessageSid"])
    files_after_first = files_under(audio_root)
    mtimes = [p.stat().st_mtime_ns for p in files_after_first]

    process_inbound(VOICE_NOTE["MessageSid"])

    assert recording_count() == 1
    assert len(fake_channel.download_calls) == 1
    assert files_under(audio_root) == files_after_first
    assert [p.stat().st_mtime_ns for p in files_after_first] == mtimes


def test_backlog_rerun_of_processed_audio_stores_recording(
    clean_db: None, fake_channel: FakeChannel
) -> None:
    # Voice notes from step 2 were marked processed without a recording.
    add_message(VOICE_NOTE, "audio")
    with db.get_sync_sessionmaker()() as session:
        session.query(InboundMessage).update({"status": "processed"})
        session.commit()

    process_inbound(VOICE_NOTE["MessageSid"])

    _, recording = load(VOICE_NOTE["MessageSid"])
    assert recording is not None and recording.status == "stored"


def test_delete_media_called_once_after_stored_when_enabled(
    clean_db: None, fake_channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "twilio_delete_media", True)
    add_message(VOICE_NOTE, "audio")

    process_inbound(VOICE_NOTE["MessageSid"])
    process_inbound(VOICE_NOTE["MessageSid"])

    assert fake_channel.delete_calls == [(VOICE_NOTE["MessageSid"], "stored")]
    _, recording = load(VOICE_NOTE["MessageSid"])
    assert recording is not None and recording.twilio_media_deleted


def test_delete_media_not_called_when_disabled(clean_db: None, fake_channel: FakeChannel) -> None:
    assert get_settings().twilio_delete_media is False
    add_message(VOICE_NOTE, "audio")

    process_inbound(VOICE_NOTE["MessageSid"])

    assert fake_channel.delete_calls == []
    _, recording = load(VOICE_NOTE["MessageSid"])
    assert recording is not None and not recording.twilio_media_deleted


def test_delete_failure_does_not_fail_job(
    clean_db: None, fake_channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "twilio_delete_media", True)

    def fail_delete(message: object) -> None:
        raise RuntimeError("channel unavailable")

    monkeypatch.setattr(fake_channel, "delete_media", fail_delete)
    add_message(VOICE_NOTE, "audio")

    process_inbound(VOICE_NOTE["MessageSid"])

    message, recording = load(VOICE_NOTE["MessageSid"])
    assert message.status == "processed"
    assert recording is not None and recording.status == "stored"
    assert not recording.twilio_media_deleted


def test_failed_download_leaves_no_files_and_marks_failed(
    clean_db: None, fake_channel: FakeChannel, audio_root: Path
) -> None:
    fake_channel.download_error = MediaDownloadError("Media download failed: HTTP 404")
    add_message(VOICE_NOTE, "audio")

    with pytest.raises(MediaDownloadError):
        process_inbound(VOICE_NOTE["MessageSid"])

    message, recording = load(VOICE_NOTE["MessageSid"])
    assert message.status == "failed"
    assert "HTTP 404" in (message.error or "")
    assert recording is not None and recording.status == "failed"
    assert "HTTP 404" in (recording.error or "")
    assert files_under(audio_root) == []


def test_failed_conversion_leaves_no_files_then_retry_succeeds(
    clean_db: None, fake_channel: FakeChannel, audio_root: Path, opus_bytes: bytes
) -> None:
    fake_channel.media = b"not audio at all"
    add_message(VOICE_NOTE, "audio")

    with pytest.raises(AudioConversionError):
        process_inbound(VOICE_NOTE["MessageSid"])
    assert files_under(audio_root) == []
    assert load(VOICE_NOTE["MessageSid"])[1].status == "failed"

    # The RQ retry reuses the same recording row and succeeds.
    fake_channel.media = opus_bytes
    process_inbound(VOICE_NOTE["MessageSid"])

    message, recording = load(VOICE_NOTE["MessageSid"])
    assert message.status == "processed"
    assert message.attempts == 2
    assert recording is not None and recording.status == "stored" and recording.error is None
    assert recording_count() == 1
    assert len(files_under(audio_root)) == 2


def test_text_message_creates_no_recording(
    clean_db: None, fake_channel: FakeChannel, audio_root: Path
) -> None:
    add_message(TEXT, "text")

    process_inbound(TEXT["MessageSid"])

    message, recording = load(TEXT["MessageSid"])
    assert message.status == "processed"
    assert recording is None
    assert fake_channel.download_calls == []
    assert files_under(audio_root) == []


# ============================================================
# Conversion
# ============================================================
def test_to_wav_raises_clear_error_on_bad_input(tmp_path: Path, opus_bytes: bytes) -> None:
    from src.audio.convert import to_wav

    bad = tmp_path / "bad.ogg"
    bad.write_bytes(b"garbage")
    with pytest.raises(AudioConversionError, match="ffmpeg exited"):
        to_wav(bad, tmp_path / "out.wav")


def test_ffmpeg_is_usable() -> None:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    assert "libopus" in result.stdout
