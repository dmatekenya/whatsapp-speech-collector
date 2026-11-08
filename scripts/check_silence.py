"""Measure loudness and speech content of 16 kHz mono WAV recordings.

Usage:
    uv run python scripts/check_silence.py data/audio/P0006/*.wav
    uv run python scripts/check_silence.py data/audio/P0006/          (all WAVs in a folder)
    uv run python scripts/check_silence.py --active-dbfs -50 data/audio/P0006/

Prints, per file: duration, overall loudness (dBFS), loudest frame, seconds of "active" audio by
the same energy check the worker uses (src/audio/levels.py), the active share, seconds of speech
according to WebRTC VAD (if installed: uv add webrtcvad-wheels), and what config clip_checks
would decide (ok, low_speech or no_speech). Settings come from config/assignment_settings.yaml
(clip_checks) unless given on the command line. Use it to choose thresholds: compare clips you
know are silent with clips you know contain speech.
"""

# =======
# IMPORTS
# =======
import argparse
import sys
from array import array
from pathlib import Path

from src.audio.levels import Wav, measure, read_wav
from src.tasks.config import get_collection_config

try:
    import webrtcvad
except ImportError:
    webrtcvad = None

# ========
# SETTINGS
# ========
VAD_MODE = 2  # WebRTC VAD aggressiveness: 0 (lenient) .. 3 (strict)
VAD_FRAME_MS = (10, 20, 30)
VAD_RATES = (8000, 16000, 32000, 48000)


# ========
# MEASURES
# ========
def vad_speech_seconds(wav: Wav, frame_ms: int) -> float | None:
    """Seconds WebRTC VAD calls speech; None if it isn't installed or can't run on this clip."""
    if webrtcvad is None or wav.sample_rate not in VAD_RATES or frame_ms not in VAD_FRAME_MS:
        return None
    if wav.full_scale != 32768:  # VAD needs 16-bit PCM
        return None
    vad = webrtcvad.Vad(VAD_MODE)
    pcm = array("h", wav.samples).tobytes()
    frame_bytes = 2 * wav.sample_rate * frame_ms // 1000
    speech = sum(
        1
        for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes)
        if vad.is_speech(pcm[start : start + frame_bytes], wav.sample_rate)
    )
    return speech * frame_ms / 1000


# ====
# MAIN
# ====
def main(argv: list[str] | None = None) -> None:
    checks = get_collection_config().clip_checks
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="WAV files or folders of WAV files")
    parser.add_argument("--frame-ms", type=int, default=checks.frame_ms)
    parser.add_argument("--active-dbfs", type=float, default=checks.active_dbfs)
    args = parser.parse_args(argv)
    checks = checks.model_copy(update={"frame_ms": args.frame_ms, "active_dbfs": args.active_dbfs})

    paths: list[Path] = []
    for p in args.paths:
        paths += sorted(p.glob("*.wav")) if p.is_dir() else [p]
    if not paths:
        sys.exit("No WAV files found.")
    print(
        f"frame {checks.frame_ms} ms, active above {checks.active_dbfs:g} dBFS;"
        f" no_speech under {checks.min_active_seconds:g} s active"
        f" (or over {checks.long_clip_seconds:g} s long and under {100 * checks.long_clip_min_active_ratio:.0f}% active),"
        f" low_speech under {100 * checks.min_active_ratio:.0f}% active"
    )
    print(
        f"{'file':28} {'secs':>5} {'dBFS':>6} {'peak':>6} {'active_s':>8} {'active%':>7}"
        f" {'speech_s':>8} {'speech%':>7}  verdict"
    )
    for p in paths:
        try:
            wav = read_wav(p)
        except ValueError as exc:
            print(f"{p.name:28} skipped: {exc}")
            continue
        m = measure(wav, checks.frame_ms, checks.active_dbfs)
        speech_s = vad_speech_seconds(wav, checks.frame_ms)
        sp = "n/a" if speech_s is None else f"{speech_s:.1f}"
        pct = "n/a" if speech_s is None else f"{100 * speech_s / max(m.duration_s, 1e-9):.0f}%"
        verdict = checks.problem(m.active_seconds, m.duration_s) or "ok"
        print(
            f"{p.name:28} {m.duration_s:5.1f} {m.mean_dbfs:6.1f} {m.peak_dbfs:6.1f} "
            f"{m.active_seconds:8.1f} {100 * m.active_ratio:6.0f}% {sp:>8} {pct:>7}  {verdict}"
        )


if __name__ == "__main__":
    main()
