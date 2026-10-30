import io
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.config import get_settings
from src.media.urls import (
    MEDIA_ROUTE,
    LocalSignedUrlBuilder,
    MediaUrlError,
    build_media_url,
    get_media_url_builder,
    sign_token,
    verify_token,
)
from src.storage import db
from src.storage.files import get_prompt_media_storage
from src.storage.models import Prompt

SECRET = "test-media-signing-secret"


# =======
# HELPERS
# =======
def jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 200, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def prepared_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "images" / "_prepared"
    root.mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "prompt_media_root", root)
    get_prompt_media_storage.cache_clear()
    yield root
    get_prompt_media_storage.cache_clear()


def add_prompt(prompt_id: str, media_path: str | None, prompt_type: str = "image", content_type: str = "image/jpeg") -> None:
    with db.get_sync_sessionmaker()() as session:
        session.add(
            Prompt(
                prompt_id=prompt_id,
                prompt_type=prompt_type,
                text=None if prompt_type == "image" else "Some sentence text here.",
                media_path=media_path,
                media_content_type=content_type if media_path else None,
                content_sha256=prompt_id,
                source_file="test",
            )
        )
        session.commit()


def url_for(prompt_id: str, ttl: int = 600) -> str:
    return f"{MEDIA_ROUTE}/{sign_token(prompt_id, int(time.time()) + ttl, SECRET)}"


# =============
# SIGNED TOKENS
# =============
def test_token_round_trip() -> None:
    token = sign_token("I0001", 2_000_000_000, SECRET)
    assert verify_token(token, SECRET, now=1_900_000_000) == "I0001"


def test_expired_token_rejected() -> None:
    token = sign_token("I0001", 1_000, SECRET)
    assert verify_token(token, SECRET, now=999) == "I0001"
    assert verify_token(token, SECRET, now=1_000) is None


@pytest.mark.parametrize(
    "tamper",
    [
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),  # signature changed
        lambda t: sign_token("I0002", 2_000_000_000, SECRET).split(".")[0] + "." + t.split(".")[1],  # payload swapped
        lambda t: t.split(".")[0],  # signature removed
        lambda t: t + ".extra",
        lambda t: "not-a-token",
        lambda t: "",
    ],
)
def test_tampered_tokens_rejected(tamper) -> None:  # noqa: ANN001
    token = sign_token("I0001", 2_000_000_000, SECRET)
    assert verify_token(tamper(token), SECRET, now=1_900_000_000) is None


def test_wrong_or_missing_secret_rejected() -> None:
    token = sign_token("I0001", 2_000_000_000, SECRET)
    assert verify_token(token, "another-secret", now=1_900_000_000) is None
    assert verify_token(token, "", now=1_900_000_000) is None
    with pytest.raises(MediaUrlError):
        sign_token("I0001", 2_000_000_000, "")


# ===========
# URL BUILDER
# ===========
def test_url_builder_round_trips() -> None:
    builder = LocalSignedUrlBuilder("https://example.ngrok-free.app/", SECRET)
    before = int(time.time())

    url = builder.build("I0042", ttl_seconds=120)

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}" == "https://example.ngrok-free.app"
    assert parsed.path.startswith(MEDIA_ROUTE + "/")
    token = parsed.path.removeprefix(MEDIA_ROUTE + "/")
    assert verify_token(token, SECRET) == "I0042"
    assert verify_token(token, SECRET, now=before + 119) == "I0042"
    assert verify_token(token, SECRET, now=before + 121) is None


def test_build_media_url_uses_settings() -> None:
    get_media_url_builder.cache_clear()
    try:
        url = build_media_url("I0001")
    finally:
        get_media_url_builder.cache_clear()
    assert url.startswith(os.environ["PUBLIC_BASE_URL"] + MEDIA_ROUTE + "/")


def test_url_builder_needs_base_url_and_secret() -> None:
    with pytest.raises(MediaUrlError, match="PUBLIC_BASE_URL"):
        LocalSignedUrlBuilder("", SECRET).build("I0001")
    with pytest.raises(MediaUrlError, match="MEDIA_SIGNING_SECRET"):
        LocalSignedUrlBuilder("https://example.org", "").build("I0001")


# =====
# ROUTE
# =====
def test_valid_token_serves_the_prepared_file(client: TestClient, prepared_root: Path) -> None:
    data = jpeg_bytes()
    (prepared_root / "food").mkdir()
    (prepared_root / "food" / "I0001.jpg").write_bytes(data)
    add_prompt("I0001", "food/I0001.jpg")

    response = client.get(url_for("I0001"))

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'filename="I0001.jpg"' in response.headers["content-disposition"]

    head = client.head(url_for("I0001"))
    assert head.status_code == 200 and head.content == b""


def test_failures_all_return_the_same_404(client: TestClient, prepared_root: Path) -> None:
    (prepared_root / "food").mkdir()
    (prepared_root / "food" / "I0001.jpg").write_bytes(jpeg_bytes())
    add_prompt("I0001", "food/I0001.jpg")
    add_prompt("S00001", None, prompt_type="sentence")
    add_prompt("I0002", "food/I0002.jpg")  # file missing
    good = url_for("I0001")

    paths = {
        "expired": f"{MEDIA_ROUTE}/{sign_token('I0001', int(time.time()) - 1, SECRET)}",
        "tampered": good[:-3] + ("xyz" if not good.endswith("xyz") else "abc"),
        "wrong secret": f"{MEDIA_ROUTE}/{sign_token('I0001', int(time.time()) + 600, 'other')}",
        "unknown prompt": url_for("I9999"),
        "not a media prompt": url_for("S00001"),
        "missing file": url_for("I0002"),
        "garbage": f"{MEDIA_ROUTE}/garbage",
    }
    responses = {name: client.get(path) for name, path in paths.items()}

    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(paths, 404)
    assert len({r.content for r in responses.values()}) == 1  # nothing reveals which


def test_only_prepared_files_can_be_served(client: TestClient, prepared_root: Path) -> None:
    images = prepared_root.parent
    original = images / "batch" / "food" / "original.jpg"
    original.parent.mkdir(parents=True)
    original.write_bytes(jpeg_bytes())
    secret_file = images.parent / "secret.txt"
    secret_file.write_text("not for Twilio")
    (prepared_root / "link.jpg").symlink_to(original)
    # Rows pointing outside the prepared root, however they got there.
    add_prompt("I0001", "../batch/food/original.jpg")
    add_prompt("I0002", str(original))  # absolute path
    add_prompt("I0003", "link.jpg")  # symlink out of the root
    add_prompt("I0004", "../../secret.txt", content_type="image/jpeg")
    add_prompt("I0005", ".")  # the root folder itself

    for prompt_id in ("I0001", "I0002", "I0003", "I0004", "I0005"):
        assert client.get(url_for(prompt_id)).status_code == 404, prompt_id
    # The route takes one token segment; it can't be used to walk paths.
    assert client.get(f"{MEDIA_ROUTE}/../../secret.txt").status_code == 404
    assert client.get(f"{MEDIA_ROUTE}/a/b").status_code == 404


def test_route_disabled_without_a_secret(
    client: TestClient, prepared_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (prepared_root / "I0001.jpg").write_bytes(jpeg_bytes())
    add_prompt("I0001", "I0001.jpg")
    path = url_for("I0001")
    assert client.get(path).status_code == 200

    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "media_signing_secret", SecretStr(""))
    assert client.get(path).status_code == 404
