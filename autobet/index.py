"""The bookmaker's board, walked into the index a tip is looked up in."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog

from autobet.connection import Connection, connected
from autobet.matching import (
    IndexedEvent,
    IndexedTournament,
    find_event,
    indexed_event,
    market_options,
    outcome_label,
    outcome_options,
    pick_offer,
    read_offer,
    same_teams,
    unread_fixtures,
)
from autobet.models import LegResolution, SelectionStatus, Tip, TipLeg
from autobet.storage import Store

log = structlog.get_logger(__name__)

_DISCIPLINES_TOPIC = "disciplines/NOT_LIVE/NOT_VIRTUAL/NOT_SIMULATED"
_TREE_TOPIC = "tournament-with-children"
_LANGUAGES = ("hu", "en")

ReadLeg = Callable[[TipLeg, str, Sequence[str]], Awaitable[int | None]]
"""Which of the book's entries a leg means: the leg, what is asked, the entries."""


def _upcoming(tournament: dict[str, Any]) -> int:
    """How many fixtures a tournament says it still has to play."""
    return int(tournament.get("numberOfUpcomingMatches") or 0)


def _sport_ids(records: list[dict[str, Any]]) -> list[str]:
    """Every sport id to walk, including the games a parent sport holds."""
    return [
        str(one)
        for record in records
        if record["_type"] == "SPORT"
        for one in (record["id"], *(record.get("childrenIds") or ()))
    ]


class Index:
    """The stored board: walked by the scheduled task, read by every tip."""

    def __init__(self, store: Store) -> None:
        """Hold what it takes to walk the board and to read what was walked."""
        self._store = store

    async def rebuild(self) -> int:
        """Walk every tournament of every sport and replace the stored index."""
        async with connected(await self._store.books.config()) as connection:
            sports = _sport_ids(await connection.dump(_DISCIPLINES_TOPIC))
            events: list[IndexedEvent] = []
            walked: list[IndexedTournament] = []
            declared = 0
            for sport in sports:
                tournaments = [
                    record
                    for record in await connection.dump(f"tournaments/{sport}")
                    if record["_type"] == "TOURNAMENT" and record.get("numberOfEvents")
                ]
                declared += sum(_upcoming(one) for one in tournaments)
                for tournament in tournaments:
                    tournament_id = str(tournament["id"])
                    events += [
                        indexed_event(records, tournament_id)
                        for records in await self._fixtures(connection, tournament_id)
                    ]
                    walked.append(
                        IndexedTournament(tournament_id, sport, _upcoming(tournament))
                    )

        await self._store.events.replace(events, walked)

        log.info("index_built", events=len(events), sports=len(sports), declared=declared)

        return len(events)

    async def resolve(
        self, connection: Connection, tip: Tip, read: ReadLeg
    ) -> list[LegResolution]:
        """Map every leg of a tip to the offer that would be staked."""
        events = await self._store.events.all()
        found = [
            await self._resolve_leg(connection, events, leg, read) for leg in tip.legs
        ]
        missing = [
            leg
            for leg, resolution in zip(tip.legs, found, strict=True)
            if resolution.status is SelectionStatus.NO_EVENT and leg.sport
        ]

        for sport in {leg.sport for leg in missing}:
            rewalked = await self._rewalk(connection, events, sport)

            if rewalked is None:
                continue

            events = rewalked
            found = [
                await self._resolve_leg(connection, events, leg, read)
                if resolution.status is SelectionStatus.NO_EVENT
                else resolution
                for leg, resolution in zip(tip.legs, found, strict=True)
            ]

        return [
            await self._read_event(connection, events, leg, read)
            if resolution.status is SelectionStatus.NO_EVENT
            else resolution
            for leg, resolution in zip(tip.legs, found, strict=True)
        ]

    async def _resolve_leg(
        self,
        connection: Connection,
        events: list[IndexedEvent],
        leg: TipLeg,
        read: ReadLeg,
    ) -> LegResolution:
        """Find the one betting offer a leg names, or say how far it got."""
        event = find_event(events, leg.event, leg.sport)
        if event is None:
            return LegResolution(SelectionStatus.NO_EVENT)

        return await self._on_fixture(connection, event, leg, read)

    async def _read_event(
        self,
        connection: Connection,
        events: list[IndexedEvent],
        leg: TipLeg,
        read: ReadLeg,
    ) -> LegResolution:
        """A fixture the names could not find, read off the book's own list."""
        fixtures = unread_fixtures(events, leg)

        if not fixtures:
            return LegResolution(SelectionStatus.NO_EVENT)

        choice = await read(leg, "fixture", [one.name for one in fixtures])

        if choice is None or not same_teams(leg, fixtures[choice]):
            return LegResolution(SelectionStatus.NO_EVENT)

        return await self._on_fixture(connection, fixtures[choice], leg, read)

    async def _on_fixture(
        self, connection: Connection, event: IndexedEvent, leg: TipLeg, read: ReadLeg
    ) -> LegResolution:
        """The offer a leg names on its fixture, by the names and then by the model."""
        records = await connection.match_odds(event.id)

        if not records:
            log.info("event_has_no_odds", fixture=event.name)

            return LegResolution(SelectionStatus.NO_ODDS)

        picked = pick_offer(leg, event, records)

        if picked.offer is not None:
            return picked

        return await self._read_offer(leg, event, records, read) or picked

    async def _read_offer(
        self,
        leg: TipLeg,
        event: IndexedEvent,
        records: list[dict[str, Any]],
        read: ReadLeg,
    ) -> LegResolution | None:
        """A market and outcome the names could not settle, read off the book's list."""
        markets, fitted = market_options(leg, event, records)
        choice = (
            0
            if fitted and len(markets) == 1
            else await read(
                leg, f"market of {event.name}", [str(one["name"]) for one in markets]
            )
        )

        if choice is None:
            return None

        market = markets[choice]
        outcomes = outcome_options(market, records)
        picked = await read(
            leg,
            f"outcome of {market['name']} on {event.name}",
            [outcome_label(one) for one in outcomes],
        )

        if picked is None:
            return None

        return read_offer(leg, event, market, outcomes[picked], records)

    async def _rewalk(
        self, connection: Connection, events: list[IndexedEvent], sport: str
    ) -> list[IndexedEvent] | None:
        """Re-read the tournaments of one sport the board now disagrees with."""
        wanted = await self._ids_for(connection, sport)

        if not wanted:
            return None

        held = await self._store.events.upcoming()
        grown = [
            record
            for one in wanted
            for record in await connection.dump(f"tournaments/{one}")
            if record["_type"] == "TOURNAMENT"
            and _upcoming(record) != held.get(str(record["id"]), 0)
        ]

        if not grown:
            log.info("sport_unchanged", sport=sport)

            return None

        fresh: list[IndexedEvent] = []
        rewalked: list[IndexedTournament] = []
        for tournament in grown:
            tournament_id = str(tournament["id"])
            fresh += [
                indexed_event(records, tournament_id)
                for records in await self._fixtures(connection, tournament_id)
            ]
            rewalked.append(
                IndexedTournament(
                    tournament_id,
                    str(tournament.get("sportId") or ""),
                    _upcoming(tournament),
                )
            )

        await self._store.events.update(fresh, rewalked)
        replaced = {tournament.id for tournament in rewalked}

        log.info("sport_rewalked", sport=sport, tournaments=len(grown), events=len(fresh))

        return [event for event in events if event.tournament_id not in replaced] + fresh

    async def _ids_for(self, connection: Connection, sport: str) -> list[str]:
        """The feed ids a sport is listed under, as cheaply as they can be had."""
        walked = await self._store.events.sports_of(sport)

        if walked:
            return walked

        return _sport_ids(
            [
                record
                for record in await connection.dump(_DISCIPLINES_TOPIC)
                if record["_type"] == "SPORT" and record["name"] == sport
            ]
        )

    async def _matches(
        self, connection: Connection, tournament: str
    ) -> list[list[dict[str, Any]]]:
        """One fixture's records in every language, merged on the fixture id."""
        found: dict[str, list[dict[str, Any]]] = {}
        for language in _LANGUAGES:
            for record in await connection.dump(f"matches/{tournament}", language):
                if record["_type"] == "MATCH":
                    found.setdefault(str(record["id"]), []).append(record)

        return list(found.values())

    async def _fixtures(
        self, connection: Connection, tournament: str
    ) -> list[list[dict[str, Any]]]:
        """Every fixture of a competition, across every tournament it splits into."""
        found: list[list[dict[str, Any]]] = []
        for record in await connection.dump(f"{_TREE_TOPIC}/{tournament}"):
            if record["_type"] == "TOURNAMENT":
                found += await self._matches(connection, str(record["id"]))

        return found
