"""Where bet suggestions come from."""

from collections.abc import AsyncIterator, Callable
from typing import Protocol

from autobet.config import Settings
from autobet.models import IncomingMessage
from autobet.sources.telegram import TelegramSource


class TipSource(Protocol):
    """A stream of messages that may contain bet suggestions."""

    name: str

    async def start(self) -> None:
        """Connect and subscribe. Called once, before ``messages``."""
        ...

    def messages(self) -> AsyncIterator[IncomingMessage]:
        """Yield messages as they arrive, forever."""
        ...

    def healthy(self) -> bool:
        """Whether the source is currently live."""
        ...

    async def stop(self) -> None:
        """Disconnect."""
        ...


SOURCES: dict[str, Callable[[Settings], TipSource]] = {
    "telegram": TelegramSource,
}


def build_sources(settings: Settings) -> list[TipSource]:
    """Build every source named in the ``SOURCES`` setting."""
    return [SOURCES[name](settings) for name in settings.sources]
