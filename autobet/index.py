"""The bookmaker's board, walked into the index a tip is looked up in.

The walk is a scheduled task (`python -m autobet index`), not a loop inside the
service: it writes to Postgres, so the index outlives a deploy and the app only
ever reads it.
"""

from typing import Any

import structlog

from autobet.connection import Connection, connected
from autobet.matching import IndexedEvent, find_event, indexed_event, pick_offer
from autobet.models import LegResolution, SelectionStatus, Tip, TipLeg
from autobet.storage import Store

log = structlog.get_logger(__name__)

_DISCIPLINES_TOPIC = "disciplines/NOT_LIVE/NOT_VIRTUAL/NOT_SIMULATED"
_STAGES_TOPIC = "tournament-odds/7/1"
_LANGUAGES = ("hu", "en")


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
            upcoming: dict[str, int] = {}
            for sport in sports:
                tournaments = [
                    record
                    for record in await connection.dump(f"tournaments/{sport}")
                    if record["_type"] == "TOURNAMENT" and record.get("numberOfEvents")
                ]
                for tournament in tournaments:
                    tournament_id = str(tournament["id"])
                    events += [
                        indexed_event(records, tournament_id)
                        for records in await self._fixtures(connection, tournament_id)
                    ]
                    upcoming[tournament_id] = _upcoming(tournament)

        await self._store.events.replace(events, upcoming)

        log.info("index_built", events=len(events), sports=len(sports))

        return len(events)

    async def resolve(self, connection: Connection, tip: Tip) -> list[LegResolution]:
        """Map every leg of a tip to the offer that would be staked."""
        events = await self._store.events.all()
        found = [await self._resolve_leg(connection, events, leg) for leg in tip.legs]
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
                resolution
                if resolution.offer is not None
                else await self._resolve_leg(connection, events, leg)
                for leg, resolution in zip(tip.legs, found, strict=True)
            ]

        return found

    async def _resolve_leg(
        self, connection: Connection, events: list[IndexedEvent], leg: TipLeg
    ) -> LegResolution:
        """Find the one betting offer a leg names, or say how far it got."""
        event = find_event(events, leg.event, leg.sport)
        if event is None:
            return LegResolution(SelectionStatus.NO_EVENT)

        records = await connection.match_odds(event.id)

        if not records:
            log.info("event_has_no_odds", fixture=event.name)

            return LegResolution(SelectionStatus.NO_ODDS)

        return pick_offer(leg, event, records)

    async def _rewalk(
        self, connection: Connection, events: list[IndexedEvent], sport: str
    ) -> list[IndexedEvent] | None:
        """Re-read the tournaments of one sport that have grown since the walk."""
        wanted = [
            record["id"]
            for record in await connection.dump(_DISCIPLINES_TOPIC)
            if record["_type"] == "SPORT" and record["name"] == sport
        ]

        if not wanted:
            return None

        held = await self._store.events.upcoming()
        grown = [
            record
            for record in await connection.dump(f"tournaments/{wanted[0]}")
            if record["_type"] == "TOURNAMENT"
            and _upcoming(record) != held.get(str(record["id"]), 0)
        ]

        if not grown:
            log.info("sport_unchanged", sport=sport)

            return None

        fresh: list[IndexedEvent] = []
        upcoming: dict[str, int] = {}
        for tournament in grown:
            tournament_id = str(tournament["id"])
            fresh += [
                indexed_event(records, tournament_id)
                for records in await self._fixtures(connection, tournament_id)
            ]
            upcoming[tournament_id] = _upcoming(tournament)

        await self._store.events.update(fresh, upcoming)

        log.info("sport_rewalked", sport=sport, tournaments=len(grown), events=len(fresh))

        return [event for event in events if event.tournament_id not in upcoming] + fresh

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

    async def _stages(self, connection: Connection, tournament: str) -> list[str]:
        """The child tournaments a competition splits its fixtures across."""
        records = await connection.dump(f"{tournament}/{_STAGES_TOPIC}")

        return [
            str(record["id"])
            for record in records
            if record["_type"] == "TOURNAMENT" and record.get("parentId") == tournament
        ]

    async def _fixtures(
        self, connection: Connection, tournament: str
    ) -> list[list[dict[str, Any]]]:
        """One tournament's fixtures, reaching into its stages when it has any."""
        found = await self._matches(connection, tournament)

        if found:
            return found

        for stage in await self._stages(connection, tournament):
            found += await self._matches(connection, stage)

        return found
