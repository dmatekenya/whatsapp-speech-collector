"""How loud a WAV is and how much of it has sound in it (energy-based, standard library only).

Used by the worker on every voice note (after converting to 16 kHz WAV), by
scripts/backfill_speech_levels.py and by scripts/check_silence.py.

The clip is cut into frames (30 ms by default). A frame whose RMS level is
above active_dbfs (-45 dBFS by default) counts as active: speech, or some
other sound. active_seconds is the number of active frames times the frame
length; a trailing part-frame is ignored. Levels are dBFS: 0 is full scale,
and digital silence is SILENCE_DBFS.
"""

import math
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

SILENCE_DBFS = -120.0
DEFAULT_FRAME_MS = 30
DEFAULT_ACTIVE_DBFS = -45.0

# Sample width in bytes -> array typecode. WAV stores 8-bit as unsigned, the rest signed.
_TYPECODES = {1: "B", 2: "h", 4: "i"}


@dataclass(frozen=True)
class Wav:
    """Mono samples as integers, with their full-scale value (32768 for 16-bit)."""

    samples: array
    sample_rate: int
    full_scale: float

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate


@dataclass(frozen=True)
class SpeechLevels:
    duration_s: float
    mean_dbfs: float  # RMS level of the whole clip
    peak_dbfs: float  # the loudest frame
    active_seconds: float

    @property
    def active_ratio(self) -> float:
        return self.active_seconds / self.duration_s if self.duration_s > 0 else 0.0


# =======
# READING
# =======
def read_wav(path: Path) -> Wav:
    """A PCM WAV (8, 16 or 32-bit), mixed down to mono. Raises ValueError for other formats."""
    try:
        with wave.open(str(path), "rb") as wav:
            width, channels, rate = wav.getsampwidth(), wav.getnchannels(), wav.getframerate()
            raw = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"{Path(path).name}: not a PCM WAV file ({exc})") from exc
    if width not in _TYPECODES:
        raise ValueError(f"{Path(path).name}: {8 * width}-bit WAV isn't supported (8, 16 or 32-bit)")
    samples = array(_TYPECODES[width], raw[: len(raw) - len(raw) % width])
    if width > 1 and sys.byteorder == "big":
        samples.byteswap()  # WAV is little-endian
    if width == 1:
        samples = array("h", (s - 128 for s in samples))
    if channels > 1:
        usable = len(samples) - len(samples) % channels
        samples = array(
            "q", (sum(samples[i : i + channels]) // channels for i in range(0, usable, channels))
        )
    return Wav(samples=samples, sample_rate=rate, full_scale=float(1 << (8 * width - 1)))


# =========
# MEASURING
# =========
def dbfs(sum_of_squares: float, count: int, full_scale: float) -> float:
    """RMS level in dBFS of count samples whose squares add up to sum_of_squares."""
    if count == 0 or sum_of_squares <= 0:
        return SILENCE_DBFS
    rms = math.sqrt(sum_of_squares / count) / full_scale
    return max(SILENCE_DBFS, 20 * math.log10(rms))


def measure(
    wav: Wav, frame_ms: int = DEFAULT_FRAME_MS, active_dbfs: float = DEFAULT_ACTIVE_DBFS
) -> SpeechLevels:
    if frame_ms <= 0:
        raise ValueError("frame_ms must be positive")
    samples = wav.samples
    frame_len = max(1, wav.sample_rate * frame_ms // 1000)
    active_frames = 0
    peak = SILENCE_DBFS
    total = 0
    for start in range(0, len(samples) - frame_len + 1, frame_len):
        frame = samples[start : start + frame_len]
        energy = math.sumprod(frame, frame)
        total += energy
        level = dbfs(energy, frame_len, wav.full_scale)
        peak = max(peak, level)
        if level > active_dbfs:
            active_frames += 1
    # The overall level includes the trailing part-frame.
    tail = samples[len(samples) - len(samples) % frame_len :]
    total += math.sumprod(tail, tail)
    return SpeechLevels(
        duration_s=wav.duration_s,
        mean_dbfs=dbfs(total, len(samples), wav.full_scale),
        peak_dbfs=peak,
        active_seconds=active_frames * frame_len / wav.sample_rate,
    )


def measure_wav(
    path: Path, frame_ms: int = DEFAULT_FRAME_MS, active_dbfs: float = DEFAULT_ACTIVE_DBFS
) -> SpeechLevels:
    return measure(read_wav(path), frame_ms, active_dbfs)
