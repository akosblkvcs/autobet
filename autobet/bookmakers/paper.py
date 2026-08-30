"""A bookmaker that records the bet and stakes nothing."""

import structlog

from autobet.config import Settings
from autobet.models import BetResult, Tip, utcnow

log = structlog.get_logger(__name__)


class PaperBookmaker:
    """Logs what would have been staked. What ``DRY_RUN`` forces."""

    name = "paper"

    def __init__(self, settings: Settings) -> None:
        """Keep the settings; there is nothing to connect to."""
        self._settings = settings

    async def start(self) -> None:
        """Nothing to warm up."""

    async def place(self, tip: Tip) -> BetResult:
        """Pretend to place the bet, immediately and successfully."""
        return BetResult(tip=tip, accepted=True, reference="paper", placed_at=utcnow())

    async def stop(self) -> None:
        """Nothing to tear down."""
