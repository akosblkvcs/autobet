"""Bookmakers are places where tips can be staked."""

from collections.abc import Callable
from typing import Protocol

from autobet.bookmakers.paper import PaperBookmaker
from autobet.config import Settings
from autobet.models import BetResult, Tip


class Bookmaker(Protocol):
    """A place where a tip can be staked."""

    name: str

    async def start(self) -> None:
        """Log in and warm anything up. Called once, before ``place``."""
        ...

    async def place(self, tip: Tip) -> BetResult:
        """Stake the tip and report what happened."""
        ...

    async def stop(self) -> None:
        """Tear down."""
        ...


BOOKMAKERS: dict[str, Callable[[Settings], Bookmaker]] = {
    "paper": PaperBookmaker,
}


def build_bookmaker(settings: Settings) -> Bookmaker:
    """Build the configured bookmaker."""
    if settings.dry_run:
        return PaperBookmaker(settings)

    return BOOKMAKERS[settings.bookmaker](settings)
