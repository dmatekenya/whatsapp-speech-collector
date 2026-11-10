"""Tier-2 metrics (metrics_version 1): pure functions, no database.

docs/methodology.md section 8, "Step 2, scoring". Audio: snr_db (WADA-SNR) and the speech
ratio; transcript: word count, words per second, compression ratio; sentences only: CER,
WER, length ratio and whether the prompt has digits. Transcripts and prompts are compared
after the training normaliser (src/qc/text.py).
"""

import math
import wave
import zlib
from pathlib import Path

import jiwer
import numpy as np

from src.qc.text import normalize_transcript

METRICS_VERSION = "1"


# =========
# WADA-SNR
# =========
# Waveform Amplitude Distribution Analysis: C. Kim and R. M. Stern, "Robust signal-to-noise
# ratio estimation based on waveform amplitude distribution analysis", Interspeech 2008,
# pp. 2598-2601 (http://www.cs.cmu.edu/~robust/Papers/KimSternIS08.pdf). The steps follow the
# reference implementation in D. Ellis's snreval (LabROSA, Columbia, MATLAB) and its widely
# used Python port: peak-normalise, clip |z| below 1e-10, G = log(E|z|) - E[log|z|], and
# interpolate G in a table of G against SNR (-20 to 100 dB in 1 dB steps). The reference's
# last step splits the energy into speech and noise with that SNR and returns
# 10*log10(speech/noise), which is the interpolated SNR itself, so it is returned directly.
#
# The table models clean speech as a gamma-distributed amplitude with shape 0.4 (as in the
# paper) plus Gaussian noise. It was regenerated from that model (Monte Carlo, 8 million
# samples, fixed seed) rather than copied: it starts at 0.4089 for -20 dB (pure Gaussian
# noise gives 0.4094; the published table starts at 0.4097) and rises smoothly towards the
# gamma limit, log(0.4) - digamma(0.4) = 1.645. It agrees with a second, independent run to
# about 0.0001 between -10 and 50 dB, where our cut-offs (10 and 20 dB) lie.
_WADA_DB = np.arange(-20, 101, dtype=np.float64)
_WADA_G = np.array([
    0.40893, 0.40894, 0.40898, 0.40908, 0.40922, 0.40933, 0.40956, 0.40991, 0.41029, 0.41102,
    0.41180, 0.41309, 0.41480, 0.41698, 0.41995, 0.42373, 0.42845, 0.43449, 0.44192, 0.45099,
    0.46175, 0.47429, 0.48877, 0.50510, 0.52330, 0.54324, 0.56540, 0.58888, 0.61386, 0.64012,
    0.66746, 0.69572, 0.72462, 0.75380, 0.78327, 0.81286, 0.84218, 0.87157, 0.90036, 0.92866,
    0.95657, 0.98376, 1.01026, 1.03606, 1.06112, 1.08537, 1.10883, 1.13163, 1.15344, 1.17457,
    1.19501, 1.21467, 1.23363, 1.25152, 1.26869, 1.28526, 1.30120, 1.31636, 1.33096, 1.34488,
    1.35832, 1.37106, 1.38327, 1.39508, 1.40615, 1.41686, 1.42713, 1.43687, 1.44619, 1.45507,
    1.46369, 1.47194, 1.47976, 1.48729, 1.49444, 1.50120, 1.50768, 1.51387, 1.51978, 1.52543,
    1.53087, 1.53612, 1.54107, 1.54577, 1.55025, 1.55459, 1.55866, 1.56255, 1.56632, 1.56991,
    1.57333, 1.57654, 1.57967, 1.58267, 1.58553, 1.58825, 1.59081, 1.59328, 1.59566, 1.59792,
    1.60004, 1.60216, 1.60417, 1.60607, 1.60788, 1.60962, 1.61127, 1.61283, 1.61433, 1.61580,
    1.61714, 1.61845, 1.61968, 1.62086, 1.62199, 1.62306, 1.62409, 1.62507, 1.62603, 1.62696,
    1.62784,
])
_EPS = 1e-10


def wada_snr(samples: np.ndarray) -> float | None:
    """WADA-SNR estimate (dB, -20 to 100) of one channel of audio; None if empty or all zero."""
    x = np.asarray(samples, dtype=np.float64)
    if x.size == 0:
        return None
    peak = float(np.abs(x).max())
    if peak == 0.0:
        return None
    z = np.abs(x / peak)
    z[z < _EPS] = _EPS
    g = math.log(max(_EPS, float(z.mean()))) - float(np.log(z).mean())
    if g <= _WADA_G[0]:
        return float(_WADA_DB[0])
    if g >= _WADA_G[-1]:
        return float(_WADA_DB[-1])
    return float(np.interp(g, _WADA_G, _WADA_DB))


def read_wav(path: Path) -> np.ndarray:
    """The samples of a 16-bit PCM WAV (the worker's 16 kHz mono output), channels averaged."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {8 * handle.getsampwidth()}-bit")
        channels = handle.getnchannels()
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float64)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data


# =====
# AUDIO
# =====
def speech_ratio(active_seconds: float | None, duration_s: float | None) -> float | None:
    """Share of the clip with sound in it (computed, not stored)."""
    if active_seconds is None or not duration_s:
        return None
    return active_seconds / duration_s


# ==========
# TRANSCRIPT
# ==========
def word_count(transcript: str | None) -> int:
    """Words in the normalised transcript."""
    return len(normalize_transcript(transcript or "").split())


def words_per_second(words: int, active_seconds: float | None) -> float | None:
    if not active_seconds:
        return None
    return words / active_seconds


def compression_ratio(transcript: str | None) -> float | None:
    """As Whisper computes it: UTF-8 bytes of the text / bytes after zlib.compress. High values
    mean repeated phrases (Whisper treats > 2.4 as suspect). None for an empty transcript."""
    text = (transcript or "").strip()
    if not text:
        return None
    data = text.encode("utf-8")
    return len(data) / len(zlib.compress(data))


# =========
# SENTENCES
# =========
def has_digits(prompt_text: str | None) -> bool:
    """The prompt contains a digit: whisper_b writes numbers as words, so CER is unreliable."""
    return any(ch.isdigit() for ch in prompt_text or "")


def cer(prompt_text: str | None, transcript: str | None) -> float | None:
    """Character error rate of the normalised transcript against the normalised prompt
    (jiwer, as in the training evaluation). None if the prompt is empty after normalising."""
    reference = normalize_transcript(prompt_text or "")
    if not reference:
        return None
    return float(jiwer.cer(reference, normalize_transcript(transcript or "")))


def wer(prompt_text: str | None, transcript: str | None) -> float | None:
    reference = normalize_transcript(prompt_text or "")
    if not reference:
        return None
    return float(jiwer.wer(reference, normalize_transcript(transcript or "")))


def length_ratio(prompt_text: str | None, transcript: str | None) -> float | None:
    """Normalised transcript characters / normalised prompt characters: well below 1 = stopped
    early, above 1 = extra speech. None if the prompt is empty after normalising."""
    reference = normalize_transcript(prompt_text or "")
    if not reference:
        return None
    return len(normalize_transcript(transcript or "")) / len(reference)
