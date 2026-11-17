"""Automatic checks on downloaded thumbnails: file hash, perceptual hash, near-duplicates
and (optionally) visible text."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

import imagehash
from PIL import Image

from .models import Candidate

log = logging.getLogger(__name__)

Downloader = Callable[[str, Path], Path]


# ==========
# THUMBNAILS
# ==========


def fetch_thumbnails(candidates: list[Candidate], thumbs_dir: Path, download: Downloader) -> None:
    """Download a thumbnail for each kept candidate and record its hashes."""
    for c in candidates:
        if c.status == "dropped":
            continue
        url = c.thumb_url or c.image_url
        if not url:
            c.drop("no_image_url")
            continue
        dest = thumbs_dir / f"{c.candidate_id}.jpg"
        try:
            download(url, dest)
            with Image.open(dest) as img:
                img.load()
                c.phash = str(imagehash.phash(img.convert("RGB")))
        except Exception as exc:  # broken file, HTTP error, unreadable image
            log.warning("Thumbnail failed for %s: %s", c.candidate_id, exc)
            c.drop("thumbnail_failed")
            continue
        c.thumb_path = str(dest)
        c.thumb_sha256 = hashlib.sha256(dest.read_bytes()).hexdigest()


# ===============
# NEAR-DUPLICATES
# ===============


def drop_near_duplicates(candidates: list[Candidate], max_distance: int) -> None:
    """Among kept candidates with similar perceptual hashes, keep the largest original."""
    kept = [c for c in candidates if c.status == "kept" and c.phash]
    kept.sort(key=lambda c: c.width * c.height, reverse=True)
    survivors: list[tuple[imagehash.ImageHash, Candidate]] = []
    for c in kept:
        h = imagehash.hex_to_hash(c.phash)
        match = next((s for sh, s in survivors if h - sh <= max_distance), None)
        if match is not None:
            c.drop(f"near_duplicate_of:{match.candidate_id}")
        else:
            survivors.append((h, c))


# ===
# OCR
# ===


def detect_text(candidates: list[Candidate], min_confidence: float = 0.5) -> None:
    """Flag visible text with EasyOCR. Flags only; nothing is dropped here.

    EasyOCR is optional (it pulls in PyTorch). Install it before using --ocr.
    """
    try:
        import easyocr
    except ImportError as exc:
        raise RuntimeError("OCR needs easyocr: uv add easyocr") from exc

    reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    for c in candidates:
        if c.status == "dropped" or not c.thumb_path:
            continue
        results = reader.readtext(c.thumb_path)
        words = [text for _, text, conf in results if conf >= min_confidence and len(text) > 1]
        c.has_text = "yes" if words else "no"
        c.detected_text = " ".join(words)[:200]
