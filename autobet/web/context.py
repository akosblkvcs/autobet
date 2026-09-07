"""What every page needs to render, shared by the routers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import humanize
from fastapi.templating import Jinja2Templates

from autobet.bookmaker import Bookmaker
from autobet.config import Settings
from autobet.models import utcnow
from autobet.pipeline import PipelineState
from autobet.storage import MessageStore
from autobet.telegram import TelegramSource

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass(frozen=True, slots=True)
class Context:
    """Everything the pages read, built once and closed over by each router."""

    settings: Settings
    store: MessageStore
    state: PipelineState
    source: TelegramSource
    bookmaker: Bookmaker

    def limits(self) -> dict[str, Any]:
        """The settings that decide whether a tip becomes a bet."""
        return {
            "stake": f"{self.settings.stake:g}",
            "max_odds_drop": f"{self.settings.max_odds_drop_percent:g}%",
            "max_event_days_ahead": f"{self.settings.max_event_days_ahead:g}",
            "quiet_hours": (
                f"{self.settings.quiet_from_hour:02d}:00-"
                f"{self.settings.quiet_until_hour:02d}:00 "
                f"{self.settings.quiet_timezone}"
            ),
        }

    def feed(self) -> dict[str, Any]:
        """What the event index holds and how stale it is."""
        indexed_at = self.bookmaker.feed.indexed_at

        return {
            "events_indexed": self.bookmaker.feed.events,
            "index_built": (
                f"{humanize.naturaldelta(utcnow() - indexed_at)} ago"
                if indexed_at
                else "not yet"
            ),
        }

    def status(self) -> dict[str, Any]:
        """This process only, read from memory so the probe never touches Postgres."""
        idle = self.state.seconds_since_last_message()

        return {
            "dry_run": self.settings.dry_run,
            "telegram_connected": self.source.healthy(),
            "uptime": humanize.naturaldelta(self.state.uptime_seconds),
            "messages_seen": self.state.processed,
            "tips_parsed": self.state.tips,
            "last_message": (
                "none yet" if idle is None else f"{humanize.naturaldelta(idle)} ago"
            ),
        }
