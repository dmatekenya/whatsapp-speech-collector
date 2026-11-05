"""scripts/make_env_production.py, with fake env files only (never the real .env)."""

import hashlib
import stat
from pathlib import Path

import pytest

from scripts.make_env_production import COPY_FROM_LOCAL, KEY_LINE, main

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".env.production.example"

FAKE_LOCAL = """\
APP_ENV=development
PUBLIC_BASE_URL=https://fake-tunnel.ngrok-free.app
TWILIO_ACCOUNT_SID=ACfakefakefakefakefakefakefakefake
TWILIO_AUTH_TOKEN="fake-auth-token-123"
TWILIO_WHATSAPP_NUMBER=+14155550000
TWILIO_DELETE_MEDIA=false
TWILIO_VALIDATE_SIGNATURE=false
POSTGRES_PASSWORD=fake-local-db-password
MEDIA_SIGNING_SECRET=fake-local-media-secret
REDIS_URL=redis://localhost:6379/0
AUDIO_ROOT=/Users/someone/data/audio
"""
FAKE_SECRETS = (
    "ACfakefakefakefakefakefakefakefake",
    "fake-auth-token-123",
    "+14155550000",
    "fake-local-db-password",
    "fake-local-media-secret",
)


# =======
# HELPERS
# =======
def values(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        match = KEY_LINE.match(line) if not line.lstrip().startswith("#") else None
        if match:
            result[match["key"]] = match["value"]
    return result


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    local = tmp_path / ".env"
    local.write_text(FAKE_LOCAL)
    return local, tmp_path / ".env.production"


def run(local: Path, output: Path, *extra: str, template: Path = TEMPLATE) -> int:
    return main(["--env", str(local), "--output", str(output), "--template", str(template), *extra])


# =====
# TESTS
# =====
def test_copies_only_twilio_keys_and_generates_secrets(files: tuple[Path, Path], capsys: pytest.CaptureFixture) -> None:
    local, output = files
    template_hash = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()

    assert run(local, output) == 0

    result = values(output)
    assert result["TWILIO_ACCOUNT_SID"] == "ACfakefakefakefakefakefakefakefake"
    assert result["TWILIO_AUTH_TOKEN"] == '"fake-auth-token-123"'  # copied exactly as written
    assert result["TWILIO_WHATSAPP_NUMBER"] == "+14155550000"
    assert result["TWILIO_DELETE_MEDIA"] == "false"
    # Never copied: these keep the template's (or a new) value.
    assert result["TWILIO_VALIDATE_SIGNATURE"] == "true"
    assert result["APP_ENV"] == "production"
    assert result["PUBLIC_BASE_URL"] == "https://bot.zazoai.com"
    assert result["DOMAIN"] == "bot.zazoai.com"
    assert result["POSTGRES_PASSWORD"] not in ("", "fake-local-db-password")
    assert result["MEDIA_SIGNING_SECRET"] not in ("", "fake-local-media-secret")
    assert len(result["POSTGRES_PASSWORD"]) == 43 and len(result["MEDIA_SIGNING_SECRET"]) == 43
    assert "REDIS_URL" not in result and "AUDIO_ROOT" not in result  # not in the template

    # Same comments, blank lines and order as the template; only values differ.
    template_lines, output_lines = TEMPLATE.read_text().splitlines(), output.read_text().splitlines()
    assert len(template_lines) == len(output_lines)
    for before, after in zip(template_lines, output_lines, strict=True):
        if KEY_LINE.match(before) and not before.lstrip().startswith("#"):
            assert KEY_LINE.match(after)["key"] == KEY_LINE.match(before)["key"]
        else:
            assert after == before
    assert hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() == template_hash  # untouched
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    printed = capsys.readouterr().out
    for secret in (*FAKE_SECRETS, result["POSTGRES_PASSWORD"], result["MEDIA_SIGNING_SECRET"]):
        assert secret not in printed
    assert "TWILIO_AUTH_TOKEN" in printed and "copied" in printed and "generated" in printed


def test_new_secrets_each_time(files: tuple[Path, Path], tmp_path: Path) -> None:
    local, output = files
    run(local, output)
    second = tmp_path / "second.env"
    run(local, second)
    assert values(output)["MEDIA_SIGNING_SECRET"] != values(second)["MEDIA_SIGNING_SECRET"]


def test_missing_keys_and_placeholders_reported(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    local = tmp_path / ".env"
    local.write_text("TWILIO_ACCOUNT_SID=your-account-sid\n")
    output = tmp_path / ".env.production"

    run(local, output)

    printed = capsys.readouterr().out
    assert "TWILIO_AUTH_TOKEN" in printed and "missing in .env" in printed
    placeholder_section = printed.split("placeholder-like")[1]
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_NUMBER", "SUPPORT_CONTACT"):
        assert key in placeholder_section
    assert "MEDIA_SIGNING_SECRET" not in placeholder_section
    assert "your-account-sid" not in printed


def test_dry_run_writes_nothing(files: tuple[Path, Path]) -> None:
    local, output = files
    assert run(local, output, "--dry-run") == 0
    assert not output.exists()


def test_refuses_to_overwrite_without_force(files: tuple[Path, Path]) -> None:
    local, output = files
    output.write_text("EXISTING=1\n")

    with pytest.raises(SystemExit):
        run(local, output)
    assert output.read_text() == "EXISTING=1\n"

    run(local, output, "--force")
    backup = output.with_name(".env.production.bak")
    assert backup.read_text() == "EXISTING=1\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "TWILIO_ACCOUNT_SID" in output.read_text()


def test_database_url_kept_consistent_with_new_password(files: tuple[Path, Path], tmp_path: Path) -> None:
    local, output = files
    template = tmp_path / "template.env"
    template.write_text("POSTGRES_PASSWORD=\nDATABASE_URL=postgresql://speech:old@postgres:5432/speech_collector\n")

    run(local, output, template=template)

    result = values(output)
    assert result["DATABASE_URL"] == f"postgresql://speech:{result['POSTGRES_PASSWORD']}@postgres:5432/speech_collector"


def test_copy_list_never_includes_server_only_settings() -> None:
    assert set(COPY_FROM_LOCAL) == {
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_NUMBER",
        "TWILIO_DELETE_MEDIA",
    }
