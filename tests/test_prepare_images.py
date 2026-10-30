import io

import pytest
from PIL import Image, ImageCms
from PIL.PngImagePlugin import PngInfo

from src.media.images import ImageProcessingError, jpeg_metadata_segments, prepare_image

GPS_IFD = 0x8825
ORIENTATION = 0x0112
XMP = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>'


# ============================================================
# Helpers
# ============================================================
def assert_no_metadata(data: bytes) -> Image.Image:
    """The prepared file is an RGB JPEG with no metadata of any kind."""
    assert jpeg_metadata_segments(data) == []
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG" and image.mode == "RGB"
    assert len(image.getexif()) == 0
    for key in ("exif", "xmp", "icc_profile", "comment", "photoshop"):
        assert key not in image.info
    return image


def png_with_text_chunks() -> bytes:
    info = PngInfo()
    info.add_text("Author", "Jane Banda")
    info.add_text("Location", "Zomba, Malawi")
    info.add_itxt("Description", "photo of the market", lang="en")
    info.add_text("XML:com.adobe.xmp", XMP.decode())
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # camera make
    buffer = io.BytesIO()
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGBA", (300, 200), (10, 120, 200, 128)).save(
        buffer, "PNG", pnginfo=info, exif=exif, icc_profile=srgb
    )
    return buffer.getvalue()


def jpeg_with_gps(orientation: int = 1, size: tuple[int, int] = (400, 300)) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[ORIENTATION] = orientation
    exif.get_ifd(GPS_IFD).update({1: "S", 2: (15.0, 23.0, 10.0), 3: "E", 4: (35.0, 19.0, 5.0)})
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, "JPEG", exif=exif, xmp=XMP, comment=b"shot at home")
    return buffer.getvalue()


# ============================================================
# Metadata
# ============================================================
def test_png_text_chunks_exif_xmp_and_icc_removed() -> None:
    source = png_with_text_chunks()
    original = Image.open(io.BytesIO(source))
    assert {"Author", "Location", "Description"} <= set(original.info)
    assert "icc_profile" in original.info and len(original.getexif()) > 0

    prepared = prepare_image(source)

    image = assert_no_metadata(prepared.data)
    assert (prepared.width, prepared.height) == image.size == (300, 200)
    assert prepared.content_type == "image/jpeg" and prepared.extension == "jpg"
    for text in (b"Jane Banda", b"Zomba", b"PhoneMaker", b"xmpmeta"):
        assert text not in prepared.data


def test_jpeg_gps_exif_xmp_and_comment_removed() -> None:
    source = jpeg_with_gps()
    original = Image.open(io.BytesIO(source))
    assert original.getexif().get_ifd(GPS_IFD)
    assert jpeg_metadata_segments(source)  # APP1 (EXIF, XMP), COM

    prepared = prepare_image(source)

    assert_no_metadata(prepared.data)
    for text in (b"PhoneMaker", b"shot at home", b"xmpmeta", b"GPS"):
        assert text not in prepared.data


def test_exif_orientation_applied_before_stripping() -> None:
    # Orientation 6: stored landscape, displayed rotated 90 degrees.
    prepared = prepare_image(jpeg_with_gps(orientation=6, size=(400, 300)))

    assert (prepared.width, prepared.height) == (300, 400)
    assert_no_metadata(prepared.data)


def test_transparency_flattened_onto_white() -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(buffer, "PNG")

    image = assert_no_metadata(prepare_image(buffer.getvalue()).data)

    assert all(channel > 245 for channel in image.getpixel((5, 5)))


# ============================================================
# Size
# ============================================================
@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((4000, 3000), (1600, 1200)),
        ((1200, 3200), (600, 1600)),
        ((1600, 900), (1600, 900)),
        ((640, 480), (640, 480)),  # never upscaled
    ],
)
def test_long_side_at_most_1600(size: tuple[int, int], expected: tuple[int, int]) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", size, (90, 90, 90)).save(buffer, "PNG")

    prepared = prepare_image(buffer.getvalue())

    assert (prepared.width, prepared.height) == expected


def test_quality_steps_down_to_stay_under_the_limit() -> None:
    noise = Image.effect_noise((1600, 1200), 120).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, "PNG")
    full = prepare_image(buffer.getvalue())
    assert full.quality == 85

    smaller = prepare_image(buffer.getvalue(), max_bytes=int(len(full.data) * 0.8))

    assert smaller.quality < 85 and len(smaller.data) <= len(full.data) * 0.8
    with pytest.raises(ImageProcessingError, match="still over"):
        prepare_image(buffer.getvalue(), max_bytes=1000)


# ============================================================
# Broken files
# ============================================================
@pytest.mark.parametrize(
    "data",
    [b"", b"not an image at all", jpeg_with_gps()[:300], b"\x89PNG\r\n\x1a\n" + b"\0" * 100],
    ids=["empty", "text", "truncated-jpeg", "png-header-only"],
)
def test_broken_files_raise(data: bytes) -> None:
    with pytest.raises(ImageProcessingError, match="can't be decoded as an image"):
        prepare_image(data)
