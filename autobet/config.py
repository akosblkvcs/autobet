"""Runtime configuration, loaded from the environment."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, NoDecode


def _split(value: object) -> object:
    """Turn ``a,b`` from the environment into a list."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]

    return value


Names = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_split)]
ChatIds = Annotated[tuple[int, ...], NoDecode, BeforeValidator(_split)]


class Settings(BaseSettings):
    """Everything the app needs to run, sourced from environment variables."""

    environment: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    # Which tip sources to run; keys of autobet.sources.SOURCES.
    sources: Names = ("telegram",)
    # Where bets are placed; keys of autobet.bookmakers.BOOKMAKERS.
    # Ignored while dry_run is on, which always forces the paper book.
    bookmaker: str = "paper"

    # Telegram API credentials from https://my.telegram.org -> API development tools.
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    # Session file, full path including the .session extension.
    telegram_session: Path = Path("data/telethon.session")
    # Where media files are saved.
    media_dir: Path = Path("data/media")
    # Comma-separated chat ids from `make chats`.
    telegram_source_chat_ids: ChatIds = ()

    # Postgres connection string.
    database_url: str = "postgresql://autobet:autobet@localhost:5432/autobet"

    # Bind address; must be 0.0.0.0 in prod or the proxy cannot reach it.
    http_host: str = "127.0.0.1"
    # Port the control plane listens on, matching EXPOSE in the Dockerfile.
    http_port: int = 8000
    # Shared secret for guarded pages like /; unset locks them.
    autobet_token: str = ""

    # Master safety switch: when True, nothing is ever staked for real.
    dry_run: bool = True
    # Abandon a bet when the live odds have dropped this many percent.
    max_odds_drop_percent: float = Field(default=10.0, gt=0)


def load_settings() -> Settings:
    """Build settings from the current environment."""
    return Settings()
