"""Runtime configuration, loaded from the environment."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, SecretStr, field_validator
from pydantic_core.core_schema import ValidationInfo
from pydantic_settings import BaseSettings, NoDecode

from autobet import crypto
from autobet.logs import LogLevel


def _split(value: object) -> object:
    """Turn comma separated items from the environment into a list."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]

    return value


Emails = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_split)]


class Settings(BaseSettings):
    """Everything the app needs to run, sourced from environment variables."""

    @field_validator("encryption_key")
    @classmethod
    def _usable(cls, value: str) -> str:
        """A typo here would only surface on the first credential read."""
        if value:
            crypto.key(value)

        return value

    @field_validator("oidc_issuer")
    @classmethod
    def _trusted(cls, value: str, info: ValidationInfo) -> str:
        """The code and the client secret travel to this host, so it means TLS."""
        development = info.data.get("environment") == "development"

        if value and not value.startswith("https://") and not development:
            raise ValueError("OIDC_ISSUER must be an https:// URL")

        return value

    environment: Literal["development", "production"] = "development"
    log_level: LogLevel = "INFO"
    data_dir: Path = Path("data")

    encryption_key: str = ""

    database_url: str = "postgresql://postgres:postgres@localhost:5432/autobet"

    http_host: str = "127.0.0.1"
    http_port: int = 8000

    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_redirect_url: str = ""
    admin_emails: Emails = ()

    @property
    def telegram_session(self) -> Path:
        """Telethon's SQLite session."""
        return self.data_dir / "telethon.session"

    @property
    def telegram_media_dir(self) -> Path:
        """Where screenshots land, one file per message."""
        return self.data_dir / "media"

    @property
    def market_families(self) -> Path:
        """The bet types `make markets` harvests from the book."""
        return self.data_dir / "markets.json"


def load_settings() -> Settings:
    """Build settings from the current environment."""
    return Settings()
