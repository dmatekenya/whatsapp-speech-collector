"""Prepare image prompts for sending: clean, small, metadata-free JPEGs.

prepare_image() applies the EXIF orientation, converts to sRGB RGB (alpha is
flattened onto white), shrinks the long side to at most max_long_side, and
encodes a JPEG at the given quality (stepping the quality down if needed to
stay under max_bytes). The JPEG is encoded from a new image built from raw
pixels only, so no metadata of any kind (EXIF/GPS, XMP, IPTC, C2PA/JUMBF, ICC
profiles, PNG text chunks, comments) can carry over.
"""

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

JPEG_CONTENT_TYPE = "image/jpeg"
JPEG_EXTENSION = "jpg"
MIN_QUALITY = 60
_WHITE = (255, 255, 255)


class ImageProcessingError(ValueError):
    """An image couldn't be decoded or prepared."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    width: int
    height: int
    quality: int
    content_type: str = JPEG_CONTENT_TYPE
    extension: str = JPEG_EXTENSION


# ============================================================
# Helpers
# ============================================================
def _to_srgb(image: Image.Image) -> Image.Image:
    """Convert pixels from an embedded ICC profile to sRGB, so colours survive stripping it."""
    icc = image.info.get("icc_profile")
    if not icc or image.mode not in ("RGB", "RGBA"):
        return image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        target = ImageCms.createProfile("sRGB")
        return ImageCms.profileToProfile(image, source, target, outputMode=image.mode)
    except (ImageCms.PyCMSError, OSError, ValueError):
        return image  # unusable profile: keep the pixels as they are


def _to_rgb(image: Image.Image) -> Image.Image:
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        rgba = _to_srgb(image.convert("RGBA")) if image.mode != "RGBA" else _to_srgb(image)
        background = Image.new("RGBA", rgba.size, (*_WHITE, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    if image.mode == "RGB":
        return _to_srgb(image)
    return image.convert("RGB")


# ============================================================
# Preparing
# ============================================================
def prepare_image(
    source: bytes,
    *,
    max_long_side: int = 1600,
    quality: int = 85,
    max_bytes: int = 5 * 1024 * 1024,
) -> PreparedImage:
    try:
        with warnings.catch_warnings():
            # Very large images raise DecompressionBombError past 2x the limit.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as opened:
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                rgb = _to_rgb(oriented)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ImageProcessingError(f"can't be decoded as an image ({type(exc).__name__})") from None

    rgb.thumbnail((max_long_side, max_long_side), Image.Resampling.LANCZOS)  # only shrinks
    # A fresh image from raw pixels: nothing from the original's info survives.
    clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())

    for attempt in range(quality, MIN_QUALITY - 1, -5):
        buffer = io.BytesIO()
        clean.save(buffer, "JPEG", quality=attempt, optimize=True)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return PreparedImage(data=data, width=clean.width, height=clean.height, quality=attempt)
    raise ImageProcessingError(
        f"still over {max_bytes / 1024 / 1024:g} MB as a JPEG at quality {MIN_QUALITY}"
    )


# ============================================================
# Checking
# ============================================================
def jpeg_metadata_segments(data: bytes) -> list[str]:
    """Names of metadata segments in a JPEG (APP1-APP15, COM). APP0 (JFIF) isn't metadata.

    An empty list means no EXIF/GPS, XMP, ICC, IPTC, C2PA or comments.
    """
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("not a JPEG")
    found = []
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise ValueError(f"bad JPEG marker at byte {position}")
        marker = data[position + 1]
        if marker == 0xDA:  # start of scan: no more headers
            break
        length = int.from_bytes(data[position + 2 : position + 4], "big")
        if 0xE1 <= marker <= 0xEF:
            found.append(f"APP{marker - 0xE0}")
        elif marker == 0xFE:
            found.append("COM")
        position += 2 + length
    return found
