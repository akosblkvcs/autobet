"""The bookmaker's board, walked into the index a tip is looked up in."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from websockets.exceptions import WebSocketException

from autobet.config import Settings
from autobet.connection import Connection, connected
from autobet.matching import IndexedEvent, find_event, indexed_event, pick_offer
from autobet.models import LegOffer, Tip, TipLeg

log = structlog.get_logger(__name__)

_DISCIPLINES_TOPIC = "disciplines/NOT_LIVE/NOT_VIRTUAL/NOT_SIMULATED"
_STAGES_TOPIC = "tournament-odds/7/1"
_INDEX_REFRESH_SECONDS = 4 * 60 * 60
_INDEX_WAIT_SECONDS = 150
_LANGUAGES = ("hu", "en")


def _upcoming(tournament: dict[str, Any]) -> int:
    """How many fixtures a tournament says it still has to play."""
    return int(tournament.get("numberOfUpcomingMatches") or 0)


class Feed:
    """The event index, and the lookups that turn a tip into offers to stake."""

    def __init__(self, settings: Settings) -> None:
        """Start with an empty index; :meth:`start` is what fills it."""
        self._settings = settings
        self._events: list[IndexedEvent] = []
        self._held: dict[str, int] = {}
        self._refresh: asyncio.Task[None] | None = None
        self._indexed = asyncio.Event()
        self._indexed_at: datetime | None = None

    async def start(self) -> None:
        """Start the background index."""
        self._refresh = asyncio.create_task(self._keep_fresh(), name="feed:index")

    async def stop(self) -> None:
        """Drop the background index."""
        if self._refresh is not None:
            self._refresh.cancel()

    @property
    def events(self) -> int:
        """How many events the index currently holds."""
        return len(self._events)

    @property
    def indexed_events(self) -> list[IndexedEvent]:
        """The index itself, for tools that walk it rather than search it."""
        return self._events

    async def indexed(self) -> None:
        """Wait for the first index to finish."""
        await self._indexed.wait()

    @property
    def indexed_at(self) -> datetime | None:
        """When the index last finished, or None if it never has."""
        return self._indexed_at

    def _now(self) -> datetime:
        """The current time where the tipster's hours are defined."""
        return datetime.now(ZoneInfo(self._settings.quiet_timezone))

    def _quiet(self, moment: datetime) -> bool:
        """Whether a moment falls in the hours the tipster does not post."""
        start = self._settings.quiet_from_hour
        end = self._settings.quiet_until_hour

        if start == end:
            return False

        hour = moment.hour

        return start <= hour < end if start < end else hour >= start or hour < end

    def _asleep(self) -> bool:
        """Whether we are inside the hours the tipster does not post."""
        return self._quiet(self._now())

    def _wakes(self, moment: datetime) -> datetime:
        """When the quiet window at or after `moment` ends."""
        wake = moment.replace(
            hour=self._settings.quiet_until_hour, minute=0, second=0, microsecond=0
        )

        return wake if wake > moment else wake + timedelta(days=1)

    def _until_next(self) -> float:
        """Seconds to wait before indexing again."""
        now = self._now()

        if self._quiet(now) or self._quiet(
            now + timedelta(seconds=_INDEX_REFRESH_SECONDS)
        ):
            return (self._wakes(now) - now).total_seconds()

        return _INDEX_REFRESH_SECONDS

    async def _keep_fresh(self) -> None:
        """Index on waking, then every few hours until the quiet window returns."""
        while True:
            if not self._asleep() or not self._indexed.is_set():
                try:
                    await self._reindex()
                except (OSError, WebSocketException) as error:
                    # A socket that will not open or does not last costs this
                    # walk, not the loop that would try again in a few hours.
                    log.warning("index_failed", detail=repr(error))

            due = self._until_next()

            log.info("index_due", hours=round(due / 3600, 1))

            await asyncio.sleep(due)

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

    async def _reindex(self) -> None:
        """Walk every tournament of every watched sport and note its events."""
        async with connected(self._settings) as connection:
            sports = [
                record["id"]
                for record in await connection.dump(_DISCIPLINES_TOPIC)
                if record["_type"] == "SPORT"
            ]
            events: list[IndexedEvent] = []
            for sport in sports:
                tournaments = [
                    record
                    for record in await connection.dump(f"tournaments/{sport}")
                    if record["_type"] == "TOURNAMENT" and record.get("numberOfEvents")
                ]
                for tournament in tournaments:
                    found = [
                        indexed_event(records)
                        for records in await self._fixtures(
                            connection, str(tournament["id"])
                        )
                    ]
                    self._held[str(tournament["id"])] = _upcoming(tournament)
                    events += found

            self._events = events
            self._indexed_at = datetime.now(UTC)
            self._indexed.set()
            log.info("feed_indexed", events=len(events), sports=len(sports))

    async def _rewalk(self, connection: Connection, sport: str) -> bool:
        """Re-read the tournaments of one sport that have grown since the walk."""
        wanted = [
            record["id"]
            for record in await connection.dump(_DISCIPLINES_TOPIC)
            if record["_type"] == "SPORT" and record["name"] == sport
        ]

        if not wanted:
            return False

        grown = [
            record
            for record in await connection.dump(f"tournaments/{wanted[0]}")
            if record["_type"] == "TOURNAMENT"
            and _upcoming(record) != self._held.get(str(record["id"]), 0)
        ]

        if not grown:
            log.info("sport_unchanged", sport=sport)

            return False

        fresh: list[IndexedEvent] = []
        for tournament in grown:
            found = [
                indexed_event(records)
                for records in await self._fixtures(connection, str(tournament["id"]))
            ]
            self._held[str(tournament["id"])] = _upcoming(tournament)
            fresh += found

        known = {event.id for event in fresh}
        self._events = [e for e in self._events if e.id not in known] + fresh

        log.info("sport_rewalked", sport=sport, tournaments=len(grown), events=len(fresh))

        return True

    async def _ready(self) -> None:
        """Wait for the first index, since an empty one resolves nothing."""
        if self._indexed.is_set():
            return

        log.info("waiting_for_index")
        try:
            async with asyncio.timeout(_INDEX_WAIT_SECONDS):
                await self._indexed.wait()
        except TimeoutError:
            log.warning("index_not_ready")

    async def resolve(self, connection: Connection, tip: Tip) -> list[LegOffer | None]:
        """Map every leg of a tip to the offer that would be staked."""
        await self._ready()

        offers = [await self._resolve_leg(connection, leg) for leg in tip.legs]
        missing = [
            leg
            for leg, offer in zip(tip.legs, offers, strict=True)
            if offer is None and leg.sport and find_event(self._events, leg.event) is None
        ]

        for sport in {leg.sport for leg in missing}:
            if not await self._rewalk(connection, sport):
                continue

            offers = [
                offer if offer is not None else await self._resolve_leg(connection, leg)
                for leg, offer in zip(tip.legs, offers, strict=True)
            ]

        return offers

    async def _resolve_leg(self, connection: Connection, leg: TipLeg) -> LegOffer | None:
        """Find the one betting offer a leg names, or None if anything is unclear."""
        event = find_event(self._events, leg.event)
        if event is None:
            return None

        records = await connection.match_odds(event.id)

        if not records:
            log.info("event_has_no_odds", fixture=event.name)

            return None

        return pick_offer(leg, event, records)
