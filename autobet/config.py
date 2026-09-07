"""Runtime configuration, loaded from the environment."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, NoDecode


def _split(value: object) -> object:
    """Turn comma separated items from the environment into a list."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]

    return value


ChatIds = Annotated[tuple[int, ...], NoDecode, BeforeValidator(_split)]


class Settings(BaseSettings):
    """Everything the app needs to run, sourced from environment variables."""

    environment: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: Path = Path("data/telethon.session")
    telegram_media_dir: Path = Path("data/media")
    telegram_source_chat_ids: ChatIds = ()

    claude_api_key: str = ""

    book_site: str = ""
    book_api: str = ""
    book_loader: str = ""
    book_ws: str = ""
    book_realm: str = ""
    book_operator: str = ""
    book_username: str = ""
    book_password: SecretStr = SecretStr("")

    quiet_from_hour: int = Field(default=23, ge=0, le=23)
    quiet_until_hour: int = Field(default=7, ge=0, le=23)
    quiet_timezone: str = "Europe/Budapest"

    database_url: str = "postgresql://autobet:autobet@localhost:5432/autobet"

    http_host: str = "127.0.0.1"
    http_port: int = 8000
    autobet_token: str = ""

    dry_run: bool = True
    stake: float = Field(default=100.0, gt=0)
    max_odds_drop_percent: float = Field(default=10.0, gt=0)
    max_event_days_ahead: float = Field(default=7.0, gt=0)


def load_settings() -> Settings:
    """Build settings from the current environment."""
    return Settings()
