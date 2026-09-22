"""Domain models shared across the app."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class RefusalCode(StrEnum):
    """Why a tip was not staked. The reason is a value, never a sentence."""

    LEG_UNRESOLVED = "leg_unresolved"
    EVENT_STARTED = "event_started"
    ODDS_DROP = "odds_drop"
    ODDS_RISE = "odds_rise"
    NO_ACCOUNT = "no_account"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    USER_PAUSED = "user_paused"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    STAKE_TOO_SMALL = "stake_too_small"
    PAPER_MODE = "paper_mode"
    DRY_RUN = "dry_run"
    BOOK_REJECTED = "book_rejected"
    HORIZON = "horizon"


class BetState(StrEnum):
    """What became of a bet: staked, declined by us, or blown up mid-flight."""

    PLACED = "placed"
    REFUSED = "refused"
    ERROR = "error"


class SelectionStatus(StrEnum):
    """How far a leg got towards an offer to stake."""

    RESOLVED = "resolved"
    NO_EVENT = "no_event"
    NO_MARKET = "no_market"
    NO_OUTCOME = "no_outcome"
    NO_ODDS = "no_odds"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Refusal:
    """A refusal as a code plus the numbers that produced it."""

    code: RefusalCode
    detail: str = ""


def combined(legs: Sequence[TipLeg]) -> float | None:
    """Every leg multiplied together, or None unless the tipster priced them all."""
    prices = [leg.odds for leg in legs]

    if any(price is None for price in prices):
        return None

    return math.prod(price for price in prices if price is not None)


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Role(StrEnum):
    """What a signed-in person may do here. Authelia says who they are."""

    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True, slots=True)
class User:
    """Someone the identity provider vouched for, as this app knows them."""

    id: int
    subject: str
    email: str
    role: Role

    @property
    def is_admin(self) -> bool:
        """Whether this user may change settings and see everyone's bets."""
        return self.role is Role.ADMIN


@dataclass(frozen=True, slots=True)
class Person:
    """A user as the admin page lists them: who, and whether they may in."""

    user: User
    status: str
    last_login_at: datetime | None


@dataclass(frozen=True, slots=True)
class SignedIn:
    """An open session: who it belongs to, and the token its forms must carry."""

    user: User
    csrf: str


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A single message as it reached us, whatever carried it."""

    external_id: str
    channel: str
    sent_at: datetime
    received_at: datetime
    text: str
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
    odds: float | None
    sport: str = ""


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
    def drop_percent(self) -> float | None:
        """How far the live price has fallen below the tipster's, if they gave one."""
        if self.leg.odds is None:
            return None

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
class LegResolution:
    """How far a leg got towards an offer, and the offer when it arrived."""

    status: SelectionStatus
    offer: LegOffer | None = None


@dataclass(frozen=True, slots=True)
class Tip:
    """A bet suggestion extracted from a message, shared by everyone betting it."""

    legs: tuple[TipLeg, ...]
    odds: float | None
    message: IncomingMessage


@dataclass(frozen=True, slots=True)
class Verdict:
    """What one account's bet on a tip came to, for reading only."""

    who: str = ""
    refusal: Refusal | None = None
    error: str = ""
    reference: str = ""


@dataclass(frozen=True, slots=True)
class MessageWithTip:
    """A message, its tip and what each account did about it, for reading only."""

    message: IncomingMessage
    legs: tuple[TipLeg, ...]
    resolutions: tuple[LegResolution | None, ...] = ()
    verdicts: tuple[Verdict, ...] = ()

    def paired(self) -> list[tuple[TipLeg, LegResolution | None]]:
        """Legs next to how each one resolved; None where nothing looked it up."""
        resolutions = self.resolutions or (None,) * len(self.legs)

        return list(zip(self.legs, resolutions, strict=True))

    @property
    def odds(self) -> float | None:
        """What the slip pays, or None unless the tipster priced every leg."""
        return combined(self.legs)


@dataclass(frozen=True, slots=True)
class BetResult:
    """What a bookmaker did with a tip."""

    tip: Tip
    reference: str
    placed_at: datetime
    resolutions: tuple[LegResolution, ...] = ()
    refusal: Refusal | None = None
    error: str = ""
    user_id: int | None = None
    """Whose bet this is. None while nobody holds credentials for the book."""

    stake: Decimal = Decimal("0")
    """What this account staked, or would have. Zero only on the unowned row."""

    @property
    def offers(self) -> tuple[LegOffer | None, ...]:
        """Just the offers, for the checks that only care about the price."""
        return tuple(resolution.offer for resolution in self.resolutions)

    @property
    def accepted(self) -> bool:
        """Whether the bet stands. Derived, so it cannot disagree with the reason."""
        return self.refusal is None and not self.error

    @property
    def state(self) -> BetState:
        """The column the archive stores, derived from the same two fields."""
        if self.error:
            return BetState.ERROR

        return BetState.REFUSED if self.refusal else BetState.PLACED

    @property
    def total_latency_ms(self) -> int:
        """Milliseconds from the tip being sent to the bet being placed."""
        return int((self.placed_at - self.tip.message.sent_at).total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class Placement:
    """One tip resolved once, and what each account's bet on it came to."""

    resolutions: tuple[LegResolution, ...]
    """What the legs resolved to at the book, shared by every account."""

    results: tuple[BetResult, ...]
    """One per account, or one unowned result when nobody holds credentials."""
