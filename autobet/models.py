"""Domain models shared across the app."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime

FAILED = "failed: "


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
    def chat_id(self) -> int:
        """The chat this came from; ``external_id`` is "{chat_id}:{message_id}"."""
        return int(self.external_id.split(":", 1)[0])

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
    market_id: str
    outcome_id: str
    betting_type_id: str
    offer_id: str
    odds: float
    starts_at: datetime | None = None

    @property
    def drop_percent(self) -> float:
        """How far the live price has fallen below the one on the screenshot."""
        return (self.leg.odds - self.odds) / self.leg.odds * 100

    @property
    def days_ahead(self) -> float:
        """How far off the event is, or 0.0 when the feed gave no start time."""
        if self.starts_at is None:
            return 0.0

        return (self.starts_at - utcnow()).total_seconds() / 86400

    @property
    def started(self) -> bool:
        """Whether kick-off has passed, which makes every market an in-play one."""
        return self.starts_at is not None and self.starts_at < utcnow()


@dataclass(frozen=True, slots=True)
class Tip:
    """A bet suggestion extracted from a message."""

    legs: tuple[TipLeg, ...]
    odds: float
    stake: float
    message: IncomingMessage


@dataclass(frozen=True, slots=True)
class MessageWithTip:
    """A message, its tip and what the bookmaker did, joined for reading only."""

    message: IncomingMessage
    legs: tuple[TipLeg, ...]
    offers: tuple[LegOffer | None, ...] = ()
    refusal: str = ""
    reference: str = ""

    def paired(self) -> list[tuple[TipLeg, LegOffer | None]]:
        """Legs next to what each one resolved to, for rendering."""
        offers = self.offers or (None,) * len(self.legs)

        return list(zip(self.legs, offers, strict=True))

    @property
    def failed(self) -> bool:
        """Whether the tip blew up before it was judged, so no leg was looked up."""
        return self.refusal.startswith(FAILED)

    @property
    def odds(self) -> float:
        """What the whole slip pays: every leg multiplied together."""
        return math.prod(leg.odds for leg in self.legs)


@dataclass(frozen=True, slots=True)
class BetResult:
    """What a bookmaker did with a tip."""

    tip: Tip
    reference: str
    placed_at: datetime
    offers: tuple[LegOffer | None, ...] = ()
    refusal: str = ""

    @property
    def accepted(self) -> bool:
        """Whether the bet stands. Derived, so it cannot disagree with the reason."""
        return not self.refusal

    @property
    def total_latency_ms(self) -> int:
        """Milliseconds from the tip being sent to the bet being placed."""
        return int((self.placed_at - self.tip.message.sent_at).total_seconds() * 1000)
