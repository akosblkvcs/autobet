"""Rows as the domain wants them: one converter per table."""

from typing import Protocol

from asyncpg import Record

from autobet.matching import IndexedEvent
from autobet.models import (
    IncomingMessage,
    LegOffer,
    LegResolution,
    SelectionStatus,
    TipLeg,
)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class Executes(Protocol):
    """Anything a query can run on: the pool, or a connection in a transaction."""

    async def execute(
        self, query: str, *args: object, timeout: float | None = None
    ) -> str:
        """Run one statement."""
        ...

    async def fetch(
        self, query: str, *args: object, timeout: float | None = None
    ) -> list[Record]:
        """Every row one query answers."""
        ...

    async def fetchrow(
        self, query: str, *args: object, timeout: float | None = None
    ) -> Record | None:
        """The first row one query answers, or None."""
        ...


def to_message(row: Record) -> IncomingMessage:
    """A message row, with its channel's title as the carrier gave it."""
    return IncomingMessage(
        external_id=row["external_id"],
        channel=row["channel"],
        sent_at=row["sent_at"],
        received_at=row["received_at"],
        text=row["text"],
        media_path=row["media_path"],
    )


def to_leg(row: Record) -> TipLeg:
    """One leg, in the tipster's own words."""
    return TipLeg(
        event=row["event"],
        market=row["market"],
        selection=row["selection"],
        odds=None if row["odds"] is None else float(row["odds"]),
        sport=row["sport"],
    )


def to_event(row: Record) -> IndexedEvent:
    """One indexed fixture, with every name the book has for its two sides."""
    return IndexedEvent(
        id=row["id"],
        tournament_id=row["tournament_id"],
        name=row["name"],
        sport=row["sport"],
        home_id=row["home_id"],
        away_id=row["away_id"],
        home=tuple(row["home"]),
        away=tuple(row["away"]),
        starts_at=row["starts_at"],
    )


def event_row(event: IndexedEvent) -> tuple[object, ...]:
    """One event as the insert's parameters."""
    return (
        event.id,
        event.tournament_id,
        event.name,
        event.sport,
        event.home_id,
        event.away_id,
        list(event.home),
        list(event.away),
        event.starts_at,
    )


def to_resolution(row: Record, leg: TipLeg) -> LegResolution | None:
    """Rebuild how far the leg got, or None if nothing ever looked it up."""
    if not row["status"]:
        return None

    status = SelectionStatus(row["status"])

    if not row["offer_id"]:
        return LegResolution(status)

    return LegResolution(
        status,
        LegOffer(
            leg=leg,
            event_id=row["event_id"],
            event_name=row["event_name"],
            market_id=row["market_id"],
            outcome_id=row["outcome_id"],
            betting_type_id=row["betting_type_id"],
            offer_id=row["offer_id"],
            odds=float(row["live_odds"]),
            starts_at=row["starts_at"],
        ),
    )
