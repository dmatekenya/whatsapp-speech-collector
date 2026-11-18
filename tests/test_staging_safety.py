from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.main import app
from src.channels.factory import get_channel
from src.config import get_settings
from tests.meta_payloads import voice_note


@pytest.fixture
def staging(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("POSTGRES_USER", "fixture")
    monkeypatch.setenv("POSTGRES_PASSWORD", "fixture")
    get_settings.cache_clear()
    get_channel.cache_clear()
    yield
    get_settings.cache_clear()
    get_channel.cache_clear()


@pytest.mark.parametrize("flag", [None, "false"])
def test_staging_api_requires_send_stub(staging, monkeypatch: pytest.MonkeyPatch, flag: str | None) -> None:
    if flag is None:
        monkeypatch.delenv("WHATSAPP_SENDS_DISABLED", raising=False)
    else:
        monkeypatch.setenv("WHATSAPP_SENDS_DISABLED", flag)
    with pytest.raises(ValidationError, match="Staging requires WHATSAPP_SENDS_DISABLED=true"):
        with TestClient(app):
            pass


def test_staging_stubs_provider_calls(staging, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    audio = tmp_path / "fixture.ogg"
    audio.write_bytes(b"offline audio")
    monkeypatch.setenv("WHATSAPP_SENDS_DISABLED", "true")
    monkeypatch.setenv("STAGING_META_AUDIO_FIXTURE", str(audio))

    def unexpected_provider_call(*args, **kwargs):
        pytest.fail("Staging tried to open a provider client")

    monkeypatch.setattr("src.channels.meta.MetaChannel._client", unexpected_provider_call)
    monkeypatch.setattr("src.channels.twilio.Client", unexpected_provider_call)

    recipient = "+12025550123"
    meta = get_channel("meta")
    twilio = get_channel("twilio")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    for channel in (meta, twilio):
        assert channel.send_text(recipient, "hello").startswith("stub-")
        assert channel.send_media(recipient, "https://example.invalid/prompt").startswith("stub-")
    assert meta.send_template(recipient, "welcome", "en", []).startswith("stub-")
    inbound = meta.parse_inbound({"message": voice_note(recipient.lstrip("+"))})
    assert meta.download_media(inbound) == b"offline audio"
