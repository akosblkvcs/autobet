"""Domain models shared across the app."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A single message as it reached us, whatever carried it."""

    external_id: str
    channel: str
    sent_at: datetime
    received_at: datetime
    text: str
    media_kind: str | None = None
    media_path: str | None = None

    @property
    def transport_latency_ms(self) -> int:
        """Milliseconds between the carrier accepting the message and us seeing it."""
        return int((self.received_at - self.sent_at).total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class TipLeg:
    """One selection on a betslip."""

    event: str
    market: str
    selection: str
    odds: float


@dataclass(frozen=True, slots=True)
class LegOffer:
    """What one leg of a tip maps to in the bookmaker's own feed."""

    leg: TipLeg
    event_id: str
    event_name: str
    offer_id: str
    odds: float

    @property
    def drop_percent(self) -> float:
        """How far the live price has fallen below the one on the screenshot."""
        return (self.leg.odds - self.odds) / self.leg.odds * 100


@dataclass(frozen=True, slots=True)
class Tip:
    """A bet suggestion extracted from a message."""

    legs: tuple[TipLeg, ...]
    odds: float
    stake: float
    message: IncomingMessage


@dataclass(frozen=True, slots=True)
class MessageWithTip:
    """A message paired with the tip parsed from it, for reading only."""

    message: IncomingMessage
    legs: tuple[TipLeg, ...]
    offers: tuple[LegOffer | None, ...] = ()

    def paired(self) -> list[tuple[TipLeg, LegOffer | None]]:
        """Legs next to what each one resolved to, for rendering."""
        offers = self.offers or (None,) * len(self.legs)

        return list(zip(self.legs, offers, strict=True))

    @property
    def odds(self) -> float:
        """What the whole slip pays: every leg multiplied together."""
        return math.prod(leg.odds for leg in self.legs)


@dataclass(frozen=True, slots=True)
class BetResult:
    """What a bookmaker did with a tip."""

    tip: Tip
    accepted: bool
    reference: str
    placed_at: datetime
    offers: tuple[LegOffer | None, ...] = ()

    @property
    def total_latency_ms(self) -> int:
        """Milliseconds from the tip being sent to the bet being placed."""
        return int((self.placed_at - self.tip.message.sent_at).total_seconds() * 1000)
