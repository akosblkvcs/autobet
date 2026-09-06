"""What every page needs to render, shared by the routers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from autobet.config import Settings
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

    def status(self) -> dict[str, Any]:
        """This process only, read from memory so the probe never touches Postgres."""
        idle = self.state.seconds_since_last_message()

        return {
            "dry_run": self.settings.dry_run,
            "telegram_connected": self.source.healthy(),
            "uptime_seconds": round(self.state.uptime_seconds, 1),
            "messages_seen": self.state.processed,
            "tips_parsed": self.state.tips,
            "seconds_since_last_message": None if idle is None else round(idle, 1),
        }
