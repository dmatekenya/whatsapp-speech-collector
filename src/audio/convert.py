import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFMPEG_TIMEOUT_S = 120
FFPROBE_TIMEOUT_S = 30


class AudioConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioInfo:
    duration_s: float
    sample_rate: int
    channels: int


# ============================================================
# Helpers
# ============================================================
def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AudioConversionError(f"{command[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioConversionError(f"{command[0]} timed out after {timeout}s") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()[-1000:]
        raise AudioConversionError(f"{command[0]} exited with {result.returncode}: {stderr}")
    return result


# ============================================================
# Conversion and probing
# ============================================================
def to_wav(input_path: Path, output_path: Path) -> None:
    """Convert any audio ffmpeg can read to mono 16 kHz 16-bit PCM WAV."""
    _run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-i", str(input_path),
            "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
            str(output_path),
        ],
        timeout=FFMPEG_TIMEOUT_S,
    )  # fmt: skip


def probe(path: Path) -> AudioInfo:
    result = _run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", "-select_streams", "a:0",
            str(path),
        ],
        timeout=FFPROBE_TIMEOUT_S,
    )  # fmt: skip
    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        duration = data["format"].get("duration") or stream["duration"]
        return AudioInfo(
            duration_s=float(duration),
            sample_rate=int(stream["sample_rate"]),
            channels=int(stream["channels"]),
        )
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise AudioConversionError(f"Could not read audio info from ffprobe output for {path.name}") from exc
