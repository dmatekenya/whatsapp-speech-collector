"""The speech check: measuring (src/audio/levels.py), config clip_checks, and the
worker and conversation engine end to end with generated audio.

The audio is synthetic: digital silence, faint noise, and a "speech-like"
signal (voiced syllables with gaps), sent as Opus voice notes like WhatsApp's.
"""

import math
import random
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from scripts.backfill_speech_levels import backfill
from src.audio.levels import SILENCE_DBFS, measure_wav, read_wav
from src.conversation.messages import get_catalog
from src.storage import db
from src.storage.files import LocalFileStorage
from src.storage.models import InboundMessage, Recording
from src.tasks.config import ClipChecks, CollectionConfig, get_collection_config
from tests import meta_payloads as mp
from tests.test_meta_pipeline import (  # noqa: F401 - channels is a fixture
    WA_ID,
    Channels,
    assignments,
    channels,
    config,
    from_meta,
    recordings,
)

RATE = 16000
NO_SPEECH = get_catalog().raw("problems.no_speech")


# =====
# AUDIO
# =====
def amplitude(dbfs: float) -> float:
    return 32767 * 10 ** (dbfs / 20)


def silence(seconds: float) -> list[float]:
    return [0.0] * int(seconds * RATE)


def noise(seconds: float, dbfs: float = -60, seed: int = 1) -> list[float]:
    """Gaussian noise with an RMS level of dbfs: a quiet room, nobody speaking."""
    rng = random.Random(seed)
    sigma = amplitude(dbfs)
    return [rng.gauss(0, sigma) for _ in range(int(seconds * RATE))]


def speech_like(seconds: float, seed: int = 2) -> list[float]:
    """Voiced syllables (a 140 Hz voice with harmonics, ~-20 dBFS) of 120-250 ms with
    40-120 ms gaps, over a faint noise floor: roughly how speech looks to an energy check."""
    rng = random.Random(seed)
    out = noise(seconds, dbfs=-65, seed=seed)
    i, total = 0, len(out)
    while i < total:
        length = int(rng.uniform(0.12, 0.25) * RATE)
        peak = amplitude(rng.uniform(-22, -14))
        for k in range(min(length, total - i)):
            t = k / RATE
            envelope = math.sin(math.pi * k / length)
            voice = sum(math.sin(2 * math.pi * 140 * h * t) / h for h in (1, 2, 3, 4))
            out[i + k] += peak * envelope * voice / 2
        i += length + int(rng.uniform(0.04, 0.12) * RATE)
    return out


def write_wav(path: Path, samples: list[float]) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(b"".join(int(max(-32768, min(32767, s))).to_bytes(2, "little", signed=True) for s in samples))
    return path


def voice_note(tmp_path: Path, name: str, samples: list[float]) -> bytes:
    """As WhatsApp sends it: Opus in OGG."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed, skipping audio tests")
    wav = write_wav(tmp_path / f"{name}.wav", samples)
    ogg = tmp_path / f"{name}.ogg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "libopus", "-b:a", "24k", str(ogg)],
        check=True,
        timeout=60,
    )
    return ogg.read_bytes()


# =========
# MEASURING
# =========
def test_silence_has_no_active_audio(tmp_path: Path) -> None:
    levels = measure_wav(write_wav(tmp_path / "s.wav", silence(6)))
    assert levels.active_seconds == 0 and levels.mean_dbfs == SILENCE_DBFS
    assert levels.duration_s == pytest.approx(6)


def test_faint_noise_is_measured_but_not_active(tmp_path: Path) -> None:
    levels = measure_wav(write_wav(tmp_path / "n.wav", noise(6, dbfs=-60)))
    assert levels.active_seconds == 0
    assert levels.mean_dbfs == pytest.approx(-60, abs=0.5)
    # A lower threshold counts the same noise as active: all 200 frames of 30 ms.
    assert measure_wav(tmp_path / "n.wav", active_dbfs=-70).active_seconds == pytest.approx(6.0)


def test_speech_like_audio_is_mostly_active(tmp_path: Path) -> None:
    levels = measure_wav(write_wav(tmp_path / "v.wav", speech_like(6)))
    assert levels.active_seconds >= 3 and levels.active_ratio > 0.5
    assert -30 < levels.mean_dbfs < -15


def test_stereo_and_8_bit_wavs_are_read(tmp_path: Path) -> None:
    path = tmp_path / "stereo8.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(1)
        wav.setframerate(8000)
        wav.writeframes(bytes([128 + 64, 128 + 64] * 8000))  # 1 s at half scale, both channels
    read = read_wav(path)
    assert read.sample_rate == 8000 and read.duration_s == pytest.approx(1)
    assert measure_wav(path).mean_dbfs == pytest.approx(-6.02, abs=0.01)


def test_not_a_wav_is_a_clear_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(ValueError, match="not a PCM WAV"):
        read_wav(bad)


# ======
# CONFIG
# ======
def test_rules() -> None:
    checks = ClipChecks(min_active_seconds=1.5, min_active_ratio=0.2)
    assert checks.problem(None, 10) is None  # not measured: no check
    assert checks.problem(1.49, 3) == "no_speech"
    assert checks.problem(1.5, 7.5) is None  # exactly 20%
    assert checks.problem(1.5, 7.6) == "low_speech"
    assert checks.problem(6, 10) is None


def test_the_real_config_has_the_clip_checks() -> None:
    checks = get_collection_config().clip_checks
    assert checks == ClipChecks(frame_ms=30, active_dbfs=-45, min_active_seconds=1.5, min_active_ratio=0.2)


@pytest.mark.parametrize(
    "bad",
    [{"min_active_ratio": 1.5}, {"active_dbfs": 3}, {"frame_ms": 0}, {"min_active_seconds": -1}, {"min_speech": 1}],
)
def test_invalid_clip_checks_are_refused(bad: dict) -> None:
    with pytest.raises(ValidationError):
        ClipChecks(**bad)


# ==========
# END TO END
# ==========
def open_a_sentence(client: TestClient) -> None:
    from_meta(client, mp.text(WA_ID, "Hi"))
    from_meta(client, mp.text(WA_ID, "1"))


def send(client: TestClient, channels: Channels, media: bytes) -> str:  # noqa: F811
    channels.meta.media = media
    from_meta(client, mp.voice_note(WA_ID))
    return channels.meta.sent[-1][2]


@pytest.mark.parametrize("name", ["silence", "faint_noise"])
def test_no_speech_is_rejected_and_asked_again(
    client: TestClient, channels: Channels, tmp_path: Path, name: str  # noqa: F811
) -> None:
    samples = silence(6) if name == "silence" else noise(6, dbfs=-60)
    open_a_sentence(client)

    reply = send(client, channels, voice_note(tmp_path, name, samples))

    assert any(reply.startswith(variant) for variant in NO_SPEECH), reply
    assert "Sentence 1 of 3:" in reply  # the same prompt again
    (recording,) = recordings()
    assert recording.active_seconds == 0 and recording.mean_dbfs < -55
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "no_speech")
    (first,) = assignments()
    # A retake, like a too-short clip.
    assert first.status == "sent" and first.retake_count == 1 and recording.assignment_id == first.id


def test_speech_is_accepted(client: TestClient, channels: Channels, tmp_path: Path) -> None:  # noqa: F811
    open_a_sentence(client)

    reply = send(client, channels, voice_note(tmp_path, "speech", speech_like(6)))

    assert reply.startswith("✅ Received. Sentence 2 of 3:")
    (recording,) = recordings()
    assert recording.active_seconds > 3 and recording.mean_dbfs > -30
    assert (recording.qc_status, recording.qc_reason) == ("accepted", None)
    assert assignments()[0].status == "recorded"


def test_mostly_silent_long_clip_is_accepted_but_flagged(
    client: TestClient, channels: Channels, tmp_path: Path  # noqa: F811
) -> None:
    open_a_sentence(client)

    # 4 s of speech then 16 s of quiet: enough speech, but under 20% of the clip.
    reply = send(client, channels, voice_note(tmp_path, "mostly-silent", speech_like(4) + noise(16, dbfs=-65)))

    assert reply.startswith("✅ Received. Sentence 2 of 3:")
    (recording,) = recordings()
    assert recording.duration_s == pytest.approx(20, abs=0.1)
    assert 1.5 <= recording.active_seconds < 0.2 * recording.duration_s
    assert (recording.qc_status, recording.qc_reason) == ("needs_review", "low_speech")
    assert assignments()[0].status == "recorded"  # counted


def test_thresholds_come_from_the_config(
    client: TestClient, channels: Channels, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    # The worker measures with active_dbfs, the engine applies min_active_seconds.
    lenient = config().model_copy(update={"clip_checks": ClipChecks(active_dbfs=-70)})
    monkeypatch.setattr("src.workers.jobs.get_collection_config", lambda: lenient)
    open_a_sentence(client)

    reply = send(client, channels, voice_note(tmp_path, "faint", noise(6, dbfs=-60)))

    # At -70 dBFS the faint noise counts as sound: accepted.
    assert reply.startswith("✅ Received.")
    (recording,) = recordings()
    assert recording.active_seconds > 5 and recording.qc_status == "accepted"


def test_engine_thresholds_come_from_the_config(
    client: TestClient, channels: Channels, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    strict: CollectionConfig = config().model_copy(update={"clip_checks": ClipChecks(min_active_seconds=10)})
    monkeypatch.setattr("src.conversation.engine.get_collection_config", lambda: strict)
    open_a_sentence(client)

    reply = send(client, channels, voice_note(tmp_path, "speech", speech_like(6)))

    assert any(reply.startswith(variant) for variant in NO_SPEECH), reply
    assert recordings()[0].qc_reason == "no_speech"


# ========
# BACKFILL
# ========
def add_stored_recording(storage_root: Path, n: int, samples: list[float] | None, **columns: object) -> int:
    """A stored recording, from before the speech check; samples None: its WAV is missing."""
    wav_path = f"P0001/P0001_R{n:06d}.wav"
    if samples is not None:
        (storage_root / "P0001").mkdir(exist_ok=True)
        write_wav(storage_root / wav_path, samples)
    with db.get_sync_sessionmaker()() as session:
        message = InboundMessage(message_sid=f"SM{n}", sender="+265991000001", message_type="audio", raw_payload={}, status="processed")
        session.add(message)
        session.flush()
        recording = Recording(
            inbound_message_id=message.id, status="stored", wav_path=wav_path, duration_s=len(samples or []) / RATE, **columns
        )
        session.add(recording)
        session.commit()
        return recording.id


def test_backfill_measures_old_recordings_and_leaves_qc_alone(clean_db: None, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    silent = add_stored_recording(tmp_path, 1, silence(6), qc_status="accepted")
    spoken = add_stored_recording(tmp_path, 2, speech_like(6), qc_status="needs_review", qc_reason="speaker_check_pending")
    missing = add_stored_recording(tmp_path, 3, None)
    done = add_stored_recording(tmp_path, 4, silence(6), active_seconds=4.0, mean_dbfs=-20.0)
    checks = ClipChecks()

    dry = backfill(db.get_sync_sessionmaker(), storage, checks, dry_run=True)
    assert dry.measured == 2 and dry.no_speech_ids == [silent] and dry.missing_file == [missing]
    with db.get_sync_sessionmaker()() as session:
        assert session.get(Recording, silent).active_seconds is None  # dry run: nothing saved

    result = backfill(db.get_sync_sessionmaker(), storage, checks)
    assert result.would_be == {"no_speech": 1, "ok": 1}
    with db.get_sync_sessionmaker()() as session:
        rows = {r.id: r for r in session.scalars(select(Recording))}
    assert (rows[silent].active_seconds, rows[silent].mean_dbfs) == (0, SILENCE_DBFS)
    assert rows[spoken].active_seconds > 3
    # QC is never changed.
    assert (rows[silent].qc_status, rows[silent].qc_reason) == ("accepted", None)
    assert (rows[spoken].qc_status, rows[spoken].qc_reason) == ("needs_review", "speaker_check_pending")
    # Already measured: left alone (unless --all).
    assert (rows[done].active_seconds, rows[done].mean_dbfs) == (4.0, -20.0)
    assert rows[missing].active_seconds is None

    again = backfill(db.get_sync_sessionmaker(), storage, checks, remeasure=True)
    assert again.measured == 3
    with db.get_sync_sessionmaker()() as session:
        assert session.get(Recording, done).active_seconds == 0
