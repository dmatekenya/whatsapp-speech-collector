from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

# src/config.py -> project root is one level up from src/. Anchoring .env here
# means the API, worker and scripts load it whatever directory they start in.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # ===
    # APP
    # ===
    app_env: str = "development"
    # Bot message catalog: messages/bot_messages_{bot_language}.yaml
    bot_language: str = "en"
    # Shown in the HELP message, e.g. a phone number or email for the project team.
    support_contact: str = ""
    # Externally visible base URL (e.g. the ngrok/nginx URL); Twilio signs
    # requests against this, not the URL FastAPI sees.
    public_base_url: str = ""

    # ======
    # TWILIO
    # ======
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_number: str
    twilio_validate_signature: bool = True
    # Off by default so the same messages can be re-processed during development.
    twilio_delete_media: bool = False

    # =======================
    # META WHATSAPP CLOUD API
    # =======================
    # All optional, so a Twilio-only deployment still starts. The webhook
    # (/webhooks/meta) needs the verify token and app secret; sending and
    # media downloads need the access token and phone number ID.
    meta_verify_token: SecretStr | None = None
    meta_app_secret: SecretStr | None = None
    meta_access_token: SecretStr | None = None
    meta_phone_number_id: str = ""
    meta_waba_id: str = ""
    meta_graph_version: str = "v23.0"

    # ========
    # PROVIDER
    # ========
    # Replies always go out on the channel the participant wrote to. This is
    # only the default for messages with no triggering inbound message
    # (scripts/send_test_image.py, future proactive messages).
    whatsapp_provider: Literal["twilio", "meta"] = "twilio"

    # =====
    # AUDIO
    # =====
    # Relative paths are resolved against the project root.
    audio_root: Path = PROJECT_ROOT / "data" / "audio"
    # Prepared copies of media prompts (images: I0001.png, ...). Paths in the
    # database are relative to this, so it can move. It sits inside the image
    # source tree, but its "_" prefix keeps the image importer out of it.
    prompt_media_root: Path = PROJECT_ROOT / "data" / "prompts" / "images" / "_prepared"
    # Signs the URLs Twilio fetches prompt images from (GET /media/prompts/{token}).
    # Empty disables the route (every request gets 404).
    media_signing_secret: SecretStr = SecretStr("")

    # ==================
    # POSTGRES AND REDIS
    # ==================
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    redis_url: str = "redis://localhost:6379/0"

    @field_validator("audio_root", "prompt_media_root")
    @classmethod
    def _resolve_relative_paths(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    def _database_url(self, driver: str) -> URL:
        # URL.create escapes special characters in the password.
        return URL.create(
            drivername=driver,
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def async_database_url(self) -> URL:
        """For the API (asyncpg)."""
        return self._database_url("postgresql+asyncpg")

    @property
    def sync_database_url(self) -> URL:
        """For the worker, scripts and Alembic (psycopg)."""
        return self._database_url("postgresql+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
