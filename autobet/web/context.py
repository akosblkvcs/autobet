"""What every page needs to render, shared by the routers."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from autobet.config import Settings
from autobet.pipeline import PipelineState
from autobet.sources import TipSource
from autobet.storage import MessageStore

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass(frozen=True, slots=True)
class Context:
    """Everything the pages read, built once and closed over by each router."""

    settings: Settings
    store: MessageStore
    state: PipelineState
    sources: Sequence[TipSource]

    def status(self) -> dict[str, Any]:
        """Live counters, read from memory so the probe never touches Postgres."""
        idle = self.state.seconds_since_last_message()

        return {
            "sources": {source.name: source.healthy() for source in self.sources},
            "dry_run": self.settings.dry_run,
            "uptime_seconds": round(self.state.uptime_seconds, 1),
            "processed": self.state.processed,
            "duplicates": self.state.duplicates,
            "tips": self.state.tips,
            "seconds_since_last_message": None if idle is None else round(idle, 1),
        }
