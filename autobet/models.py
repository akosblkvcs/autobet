"""Domain models shared across the app."""

from dataclasses import dataclass
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A single message as it reached us, whatever carried it."""

    # Name of the TipSource that produced this, e.g. "telegram".
    source: str
    # Unique within that source; the source decides how to build it.
    external_id: str
    # Human label for where it came from: a chat title, an inbox name.
    channel: str
    sent_at: datetime
    received_at: datetime
    text: str
    # Carrier's name for the attachment.
    media_kind: str | None = None
    # Where the downloaded attachment landed.
    media_path: str | None = None

    @property
    def transport_latency_ms(self) -> int:
        """Milliseconds between the carrier accepting the message and us seeing it."""
        return int((self.received_at - self.sent_at).total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class Tip:
    """A bet suggestion extracted from a message."""

    event: str
    market: str
    selection: str
    odds: float
    stake: float
    # The message it came from, kept for provenance and end-to-end latency.
    message: IncomingMessage


@dataclass(frozen=True, slots=True)
class BetResult:
    """What a bookmaker did with a tip."""

    tip: Tip
    accepted: bool
    # Bookmaker-side identifier for the bet, or a marker like "paper".
    reference: str
    placed_at: datetime

    @property
    def total_latency_ms(self) -> int:
        """Milliseconds from the tip being sent to the bet being placed."""
        return int((self.placed_at - self.tip.message.sent_at).total_seconds() * 1000)
