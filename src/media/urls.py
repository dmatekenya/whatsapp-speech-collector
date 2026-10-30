"""Time-limited URLs for prompt media, for Twilio to fetch when sending.

Locally (and behind ngrok/nginx) the app serves prepared images itself at
GET /media/prompts/{token}, where the token is the prompt_id and an expiry,
signed with HMAC-SHA256 (Settings.media_signing_secret). On the droplet a
Spaces presigned-URL builder can replace LocalSignedUrlBuilder behind the same
MediaUrlBuilder interface.
"""

import base64
import hashlib
import hmac
import time
from functools import lru_cache
from typing import Protocol

from src.config import get_settings

MEDIA_ROUTE = "/media/prompts"
DEFAULT_TTL_SECONDS = 3600
# Domain separation: a signature for this purpose can't be reused for another.
_PURPOSE = b"prompt-media:v1:"


class MediaUrlError(RuntimeError):
    pass


# =============
# SIGNED TOKENS
# =============
def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(payload: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode(), _PURPOSE + payload, hashlib.sha256).digest()


def sign_token(prompt_id: str, expires_at: int, secret: str) -> str:
    if not secret:
        raise MediaUrlError("MEDIA_SIGNING_SECRET is not set")
    payload = f"{prompt_id}:{expires_at}".encode()
    return f"{_b64encode(payload)}.{_b64encode(_signature(payload, secret))}"


def verify_token(token: str, secret: str, now: float | None = None) -> str | None:
    """The prompt_id if the token is genuine and unexpired, else None (no reason given)."""
    if not secret:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".")
        payload = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature)
    except ValueError:  # wrong shape or bad base64 (binascii.Error is a ValueError)
        return None
    if not hmac.compare_digest(signature, _signature(payload, secret)):
        return None
    try:
        prompt_id, expires_at = payload.decode().rsplit(":", 1)
        expires = int(expires_at)
    except ValueError:
        return None
    if (now if now is not None else time.time()) >= expires or not prompt_id:
        return None
    return prompt_id


# ============
# URL BUILDERS
# ============
class MediaUrlBuilder(Protocol):
    def build(self, prompt_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
        """A URL the channel (Twilio) can fetch the prompt's prepared media from."""
        ...


class LocalSignedUrlBuilder:
    """URLs into this app's own media route, under PUBLIC_BASE_URL."""

    def __init__(self, public_base_url: str, secret: str) -> None:
        self.public_base_url = public_base_url.rstrip("/")
        self.secret = secret

    def build(self, prompt_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
        if not self.public_base_url:
            raise MediaUrlError("PUBLIC_BASE_URL is not set")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        token = sign_token(prompt_id, int(time.time()) + ttl_seconds, self.secret)
        return f"{self.public_base_url}{MEDIA_ROUTE}/{token}"


@lru_cache
def get_media_url_builder() -> MediaUrlBuilder:
    settings = get_settings()
    return LocalSignedUrlBuilder(settings.public_base_url, settings.media_signing_secret.get_secret_value())


def build_media_url(prompt_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    return get_media_url_builder().build(prompt_id, ttl_seconds)
