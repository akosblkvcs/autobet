"""Resolve a tip against the live feed."""

import asyncio
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from rapidfuzz import fuzz
from websockets.exceptions import WebSocketException

from autobet.config import Settings
from autobet.connection import Connection, connected
from autobet.models import LegOffer, Tip, TipLeg

log = structlog.get_logger(__name__)

_DISCIPLINES_TOPIC = "disciplines/NOT_LIVE/NOT_VIRTUAL/NOT_SIMULATED"
_STAGES_TOPIC = "tournament-odds/7/1"
_INDEX_REFRESH_SECONDS = 4 * 60 * 60
_INDEX_WAIT_SECONDS = 150
_MIN_SCORE = 0.85
_MIN_GAP = 0.05
_LANGUAGES = ("hu", "en")

_FIXTURE_SIDES = re.compile(r" - | vs\.? ")
_DECIMAL_LINE = re.compile(r"(\d+)[.,](\d+)")
_SELECTION_PARTS = re.compile(r"\s*(?:/|,|\bvagy\b|\bor\b)\s*")
_SHORTHAND = re.compile(r"[1x2]+")
_TEAM_SLOT = "{csapat}"
_SIDED_LINE = re.compile(r"\(([-+]?\d+(?:[.,]\d+)?)\)")
_LINE_TOKEN = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_NUMERIC = re.compile(r"[-+]?\d+\.?\d*")
_TRANSLITERATED = str.maketrans({"j": "i", "y": "i", "w": "v", "k": "c"})


def _fold(text: str) -> str:
    """Strip accents, case and transliteration differences."""
    stripped = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in stripped if not unicodedata.combining(c))

    return plain.casefold().translate(_TRANSLITERATED).strip()


def _folded(*words: str) -> tuple[str, ...]:
    """Fold a vocabulary at import, so lookups compare like with like."""
    return tuple(_fold(word) for word in words)


_SELECTION_KEYS = dict(
    zip(
        _folded("Igen", "Nem", "Yes", "No", "Döntetlen", "Draw", "X"),
        ("yes", "no", "yes", "no", "draw", "draw", "draw"),
        strict=True,
    )
)
_DRAW_WORDS = _folded("Döntetlen", "Draw", "X")
_OVER_WORDS = _folded("Több", "Over")
_UNDER_WORDS = _folded("Kevesebb", "Under")
_SIDE_WORDS = {"1": "home", "x": "draw", "2": "away"}
_SIDE_CODES = {"home": "#HOME", "away": "#AWAY", "draw": "#D"}
_SIDE_KEYS = {
    frozenset({"home"}): "home",
    frozenset({"away"}): "away",
    frozenset({"draw"}): "draw",
    frozenset({"home", "draw"}): "home_draw",
    frozenset({"away", "draw"}): "away_draw",
    frozenset({"home", "away"}): "home_away",
}


def _normalise(name: str) -> str:
    """Reduce a market name to what the slip and the feed agree on."""
    name = name.replace("–", "-").replace("—", "-")

    return " ".join(
        _DECIMAL_LINE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", name).split()
    ).casefold()


def _shape(name: str) -> tuple[int, frozenset[float]]:
    """What a market bets on: how many things at once, and at which lines."""
    words = _normalise(name).split()

    lines = frozenset(abs(float(word)) for word in words if _NUMERIC.fullmatch(word))

    return sum(word == "+" for word in words), lines


def _asked(leg: TipLeg, event: IndexedEvent) -> list[str]:
    """The market names a leg could mean, filling a `{csapat}` left unreplaced."""
    if _TEAM_SLOT not in leg.market:
        return [leg.market]

    sides = two_sides(event.name)

    if sides is None:
        return [leg.market]

    return [leg.market.replace(_TEAM_SLOT, side) for side in sides]


def _market_score(wanted: str, candidate: str) -> float:
    """How well a tipster's market name matches one the feed lists."""
    if _shape(wanted) != _shape(candidate):
        return 0.0

    return fuzz.token_sort_ratio(_normalise(wanted), _normalise(candidate)) / 100


def _top(scored: list[tuple[float, Any]]) -> list[Any]:
    """Every candidate the score cannot separate from the best, or none at all."""
    if not scored:
        return []

    ranked = sorted(scored, key=lambda pair: pair[0], reverse=True)
    best = ranked[0][0]

    if best < _MIN_SCORE:
        return []

    return [candidate for score, candidate in ranked if best - score < _MIN_GAP]


def _best(scored: list[tuple[float, Any]]) -> Any | None:
    """The one clear winner among scored candidates, or None if there is not one."""
    top = _top(scored)

    return top[0] if len(top) == 1 else None


def _initials(folded: str) -> str:
    """Abbreviation hit helper, so `ANM` reaches `Atl. Nacional Medellin`."""
    return "".join(word[0] for word in folded.split() if word)


def two_sides(name: str) -> tuple[str, str] | None:
    """Split `A - B` or `A vs. B` into its two competitors, or None."""
    parts = [part.strip() for part in _FIXTURE_SIDES.split(name)]

    return (parts[0], parts[1]) if len(parts) == 2 else None  # noqa: PLR2004


def _score(query: str, aliases: Sequence[str]) -> float:
    """How well one competitor matches any name the feed has for that side."""
    folded = _fold(query)

    return max(
        (
            0.95 if folded == _initials(alias) else fuzz.WRatio(folded, alias) / 100
            for alias in aliases
        ),
        default=0.0,
    )


def _starts_at(event: dict[str, Any]) -> datetime | None:
    """When the feed says this event starts; its `startTime` is epoch millis."""
    when = event.get("startTime")

    return datetime.fromtimestamp(when / 1000, UTC) if when else None


@dataclass(frozen=True, slots=True)
class IndexedEvent:
    """One fixture in the index, with every name the feed gives its two sides."""

    id: str
    name: str
    sport: str
    home_id: str
    away_id: str
    home: tuple[str, ...]
    away: tuple[str, ...]
    starts_at: datetime | None

    def side_of(self, competitor: str) -> str | None:
        """Which side a name picks out, or None when it fits both or neither."""
        home, away = _score(competitor, self.home), _score(competitor, self.away)

        if max(home, away) < _MIN_SCORE or abs(home - away) < _MIN_GAP:
            return None

        return "home" if home > away else "away"


def _aliases(records: Sequence[dict[str, Any]], side: str) -> tuple[str, ...]:
    """Every name the feed gave one side of a fixture, folded and deduplicated."""
    names = {
        _fold(str(record.get(key) or ""))
        for record in records
        for key in (f"{side}ParticipantName", f"{side}ShortParticipantName")
    }

    return tuple(sorted(name for name in names if name))


def _upcoming(tournament: dict[str, Any]) -> int:
    """How many fixtures a tournament says it still has to play."""
    return int(tournament.get("numberOfUpcomingMatches") or 0)


def _indexed(records: Sequence[dict[str, Any]]) -> IndexedEvent:
    """Fold one fixture's per-language records into a single index entry."""
    first = records[0]

    return IndexedEvent(
        id=str(first["id"]),
        name=str(first.get("name") or ""),
        sport=str(first.get("sportName") or ""),
        home_id=str(first.get("homeParticipantId") or ""),
        away_id=str(first.get("awayParticipantId") or ""),
        home=_aliases(records, "home"),
        away=_aliases(records, "away"),
        starts_at=_starts_at(first),
    )


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
        """Start the background index. Sockets are opened by the work that needs them."""
        self._refresh = asyncio.create_task(self._keep_fresh(), name="feed:index")

    async def stop(self) -> None:
        """Drop the background index; every socket dies with its own work."""
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
                        _indexed(records)
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
                _indexed(records)
                for records in await self._fixtures(connection, str(tournament["id"]))
            ]
            self._held[str(tournament["id"])] = _upcoming(tournament)
            fresh += found

        known = {event.id for event in fresh}
        self._events = [e for e in self._events if e.id not in known] + fresh

        log.info("sport_rewalked", sport=sport, tournaments=len(grown), events=len(fresh))

        return True

    def find_event(self, fixture: str) -> IndexedEvent | None:
        """Which indexed event a tip's event line names, or None if unclear."""
        sides = two_sides(fixture)
        if sides is None:
            return None

        found: IndexedEvent | None = _best(
            [
                ((_score(sides[0], event.home) + _score(sides[1], event.away)) / 2, event)
                for event in self._events
            ]
        )

        if found is None:
            log.info("event_unclear", fixture=fixture, indexed=len(self._events))

        return found

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
            if offer is None and leg.sport and self.find_event(leg.event) is None
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
        event = self.find_event(leg.event)
        if event is None:
            return None

        records = await connection.match_odds(event.id)

        if not records:
            log.info("event_has_no_odds", fixture=event.name)

            return None

        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            grouped.setdefault(record["_type"], []).append(record)

        asked = _asked(leg, event)
        named = {
            market["id"]: market
            for market in _top(
                [
                    (max(_market_score(a, str(m["name"])) for a in asked), m)
                    for m in grouped.get("MARKET", [])
                ]
            )
        }
        if not named:
            log.info("market_unmatched", fixture=event.name, market=leg.market)

            return None

        market_of = {
            relation["outcomeId"]: relation["marketId"]
            for relation in grouped.get("MARKET_OUTCOME_RELATION", [])
        }
        prices = {offer["outcomeId"]: offer for offer in grouped.get("BETTING_OFFER", [])}
        wanted = _selection(leg, event)
        sides = {event.home_id: "#HOME", event.away_id: "#AWAY"}
        matched = [
            (tier, named[market_of[outcome["id"]]], outcome)
            for outcome in grouped.get("OUTCOME", [])
            if market_of.get(outcome["id"]) in named
            and outcome["id"] in prices
            and (tier := _backs(outcome, leg, wanted, sides)) is not None
        ]
        closest = min((tier for tier, _, _ in matched), default=0)
        found = [
            (market, outcome) for tier, market, outcome in matched if tier == closest
        ]

        if len(found) == 1:
            market, outcome = found[0]
            offer = prices[outcome["id"]]

            return LegOffer(
                leg=leg,
                event_id=event.id,
                event_name=event.name,
                market_id=str(market["id"]),
                outcome_id=str(outcome["id"]),
                betting_type_id=str(market.get("bettingTypeId")),
                offer_id=str(offer["id"]),
                odds=float(offer["odds"]),
                starts_at=event.starts_at,
            )

        log.info(
            "outcome_unmatched",
            fixture=event.name,
            selection=leg.selection,
            matched=len(found),
        )

        return None


def _parts(selection: str) -> list[str]:
    """The outcomes a selection names, one per part."""
    folded = _fold(selection)

    if _SHORTHAND.fullmatch(folded):
        return list(folded)

    return [part for part in _SELECTION_PARTS.split(selection) if part.strip()]


def _piece(text: str, event: IndexedEvent) -> str | None:
    """Which side of the fixture one part of a selection names."""
    folded = _fold(text)

    if folded in _DRAW_WORDS:
        return "draw"

    return _SIDE_WORDS.get(folded) or event.side_of(text)


def _over_under(folded: str) -> str | None:
    """Whether a totals selection backs the over or the under."""
    if folded.startswith(_OVER_WORDS):
        return "over"
    if folded.startswith(_UNDER_WORDS):
        return "under"

    return None


def _line(text: str) -> float:
    """One signed line, however it was punctuated."""
    return float(text.replace(",", "."))


def _lines_named(leg: TipLeg) -> frozenset[float]:
    """Every signed line the leg names, in its market or in its selection."""
    spelled = (
        _normalise(f"{leg.market} {leg.selection}").replace("(", " ").replace(")", " ")
    )

    return frozenset(
        _line(word) for word in spelled.split() if _LINE_TOKEN.fullmatch(word)
    )


def _backs(
    outcome: dict[str, Any],
    leg: TipLeg,
    wanted: tuple[str | None, str | None],
    sides: dict[str, str],
) -> int | None:
    """How specifically an outcome is the bet a leg names; lower is better."""
    key, code = wanted

    sided = _SIDED_LINE.search(str(outcome.get("translatedName") or ""))

    if sided is not None and _line(sided.group(1)) not in _lines_named(leg):
        return None

    shown = _fold(_normalise(str(outcome.get("translatedName") or "")))

    if shown and shown == _fold(_normalise(leg.selection)):
        return 0

    marked = outcome.get("code") or ""
    for participant, side in sides.items():
        if participant:
            marked = marked.replace(f"#P{participant}", side)

    if code and marked == code:
        return 1

    if key is not None and outcome.get("headerNameKey") == key:
        return 2

    return None


def _selection(leg: TipLeg, event: IndexedEvent) -> tuple[str | None, str | None]:
    """What the leg backs, as a header key and as an outcome code."""
    folded = _fold(leg.selection)
    pieces = [_piece(part, event) for part in _parts(leg.selection)]
    named = [piece for piece in pieces if piece is not None]
    whole: frozenset[str] = frozenset(named) if len(named) == len(pieces) else frozenset()

    key = _SELECTION_KEYS.get(folded) or _over_under(folded) or _SIDE_KEYS.get(whole)
    code = " / ".join(_SIDE_CODES[piece] for piece in named) if whole else None

    return key, code
