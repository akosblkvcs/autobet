"""What a tip leg actually maps to at the bookmaker."""

import asyncio
import contextlib
import json
import re
import unicodedata
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import structlog
from rapidfuzz import fuzz
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from autobet.config import Settings
from autobet.models import LegOffer, Tip, TipLeg

log = structlog.get_logger(__name__)

_WAMP_ROLES: dict[str, Any] = {
    "agent": "autobet",
    "roles": {"caller": {}, "subscriber": {}},
}
_FAILED = "__feed_error__"
_WAMP_CALL = 48
_WAMP_RESULT = 50
_WAMP_ERROR = 8

_PLACE_BET = "/sports#placeBetV2"
_DISCIPLINES_TOPIC = "disciplines/NOT_LIVE/NOT_VIRTUAL/NOT_SIMULATED"
_STAGES_TOPIC = "tournament-odds/7/1"
_INDEX_REFRESH_SECONDS = 4 * 60 * 60
_INDEX_WAIT_SECONDS = 150
_RECONNECT_SECONDS = 5
_MIN_SCORE = 0.85
_MIN_GAP = 0.05
_LANGUAGES = ("hu", "en")

_FIXTURE_SIDES = re.compile(r" - | vs\.? ")
_DECIMAL_LINE = re.compile(r"(\d+)[.,](\d+)")
_SELECTION_PARTS = re.compile(r"\s*(?:/|,|\bvagy\b|\bor\b)\s*")
_SHORTHAND = re.compile(r"[1x2]+")
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


def _describe(parts: list[Any]) -> str:
    """The human-readable half of a WAMP error frame, if it carries one."""
    for part in parts:
        if isinstance(part, dict):
            described = cast("dict[str, Any]", part)
            if described.get("desc"):
                return str(described["desc"])

    return str(parts)


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


class NotLoggedInError(RuntimeError):
    """The feed would not attach an account to the socket, so no bet can be placed."""


class FeedClosedError(RuntimeError):
    """The socket dropped, so the answer this call was waiting for is not coming."""


class Feed:
    """A kept-open connection to the odds feed, plus the event index."""

    def __init__(self, settings: Settings) -> None:
        """Start empty; :meth:`start` does the connecting."""
        self._settings = settings
        self._socket: ClientConnection | None = None
        self._request = 0
        self._events: list[IndexedEvent] = []
        self._refresh: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._indexed = asyncio.Event()
        self._connected = asyncio.Event()
        self._holders = 0
        self._holding = asyncio.Lock()
        self._indexed_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the background index. The socket opens only when there is work."""
        self._refresh = asyncio.create_task(self._keep_fresh(), name="feed:index")

    @contextlib.asynccontextmanager
    async def connected(self) -> AsyncGenerator[None]:
        """Hold a socket open for one piece of work, closing it after the last."""
        async with self._holding:
            self._holders += 1

            if self._holders == 1:
                await self._open()
                self._reader = asyncio.create_task(
                    self._read_answers(), name="feed:reader"
                )

        try:
            yield
        finally:
            async with self._holding:
                self._holders -= 1

                if self._holders == 0:
                    await self._disconnect()

    async def _disconnect(self) -> None:
        """Drop the socket and the reader that owns it, if there is one."""
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None

        self._connected.clear()

        if self._socket is None:
            return

        await self._socket.close()
        self._socket = None

        log.info("feed_closed")

    async def _open(self) -> None:
        """Open a socket and greet it, so calls may be made on it."""
        socket = await connect(self._settings.book_ws, max_size=None)

        await socket.send(json.dumps([1, self._settings.book_realm, _WAMP_ROLES]))
        await socket.recv()

        self._socket = socket
        self._connected.set()

        log.info("feed_connected")

    async def authenticate(self, ce_session: str) -> str:
        """Attach an account to this socket, so bets may be placed on it.

        Returns:
            The username the feed says we are.

        Raises:
            NotLoggedInError: The feed refused the token.
        """
        answer = await self._call_raw(
            "/sports#loginWithCeSession", {"lang": "hu", "ceSession": ce_session}
        )
        username = str(answer.get("username") or "")

        if not username:
            raise NotLoggedInError(str(answer.get("message") or "no answer"))

        log.info("feed_authenticated", username=username)

        return username

    async def place_bet(self, offers: Sequence[LegOffer], stake: float) -> dict[str, Any]:
        """Place one bet covering every leg, and return whatever the feed says."""
        return await self._call_raw(
            _PLACE_BET,
            {
                "oddsValidationType": "ACCEPT_ANY",
                "liveOddsValidationType": "ACCEPT_ANY",
                "amount": stake,
                "freeBet": False,
                "lang": "hu",
                "type": "SINGLE" if len(offers) == 1 else "MULTIPLE",
                "terminalType": "DESKTOP",
                "selections": [
                    {
                        "bettingOfferId": offer.offer_id,
                        "priceValue": offer.odds,
                        "eventId": offer.event_id,
                        "marketIds": [offer.market_id],
                        "bettingTypeId": offer.betting_type_id,
                        "outcomeId": offer.outcome_id,
                    }
                    for offer in offers
                ],
            },
        )

    async def stop(self) -> None:
        """Drop the background index and whatever socket is still open."""
        if self._refresh is not None:
            self._refresh.cancel()

        await self._disconnect()

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

    async def _call(self, topic: str) -> list[dict[str, Any]]:
        """Ask for a topic's initial dump and return its records."""
        payload = await self._call_raw("/sports#initialDump", {"topic": topic})
        records: list[dict[str, Any]] = payload.get("records", [])

        return records

    async def _call_raw(
        self, procedure: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call one procedure and wait for its own answer, over one reconnect."""
        try:
            return await self._attempt(procedure, arguments)
        except FeedClosedError:
            if procedure == _PLACE_BET:
                raise

            log.info("feed_call_retried", procedure=procedure)

            return await self._attempt(procedure, arguments)

    async def _attempt(self, procedure: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One call on the socket that is up now.

        Raises:
            FeedClosedError: The socket dropped before an answer arrived.
        """
        try:
            async with asyncio.timeout(_RECONNECT_SECONDS):
                await self._connected.wait()
        except TimeoutError as error:
            raise FeedClosedError("no socket") from error

        socket = self._socket
        assert socket is not None
        self._request += 1
        mine = self._request
        waiting: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting[mine] = waiting

        try:
            await socket.send(
                json.dumps([_WAMP_CALL, mine, {}, procedure, [], arguments])
            )
        except ConnectionClosed as error:
            self._waiting.pop(mine, None)

            raise FeedClosedError(str(error)) from error

        result = await waiting
        failure = result.pop(_FAILED, None)

        if failure is None:
            return result

        log.warning("feed_call_failed", procedure=procedure, detail=failure)

        if "logged in" in failure.lower():
            raise NotLoggedInError(failure)

        return {}

    async def _read_answers(self) -> None:
        """Own the socket: hand out every answer, and rebuild it when it drops."""
        while True:
            socket = self._socket
            assert socket is not None

            try:
                async for frame in socket:
                    self._deliver(json.loads(frame))
            except ConnectionClosed:
                pass

            self._abandon()

            while not self._connected.is_set():
                try:
                    await self._open()
                except OSError as error:
                    log.warning("feed_reconnect_failed", detail=str(error))
                    await asyncio.sleep(_RECONNECT_SECONDS)

    def _deliver(self, message: list[Any]) -> None:
        """Hand one frame to whoever asked for it."""
        if message[0] == _WAMP_RESULT:
            waiting = self._waiting.pop(message[1], None)
            if waiting is not None and not waiting.done():
                waiting.set_result(message[4])
        elif message[0] == _WAMP_ERROR:
            waiting = self._waiting.pop(message[2], None)
            if waiting is not None and not waiting.done():
                waiting.set_result({_FAILED: _describe(message[4:])})

    def _abandon(self) -> None:
        """Tell every caller still waiting that its answer is not coming."""
        self._connected.clear()

        for waiting in self._waiting.values():
            if not waiting.done():
                waiting.set_exception(FeedClosedError("socket closed"))

        self._waiting.clear()

        log.warning("feed_disconnected")

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
                except FeedClosedError:
                    log.warning("index_interrupted")

            due = self._until_next()

            log.info("index_due", hours=round(due / 3600, 1))

            await asyncio.sleep(due)

    async def _matches(self, tournament: str) -> list[list[dict[str, Any]]]:
        """One fixture's records per language, asked for at the same time."""
        operator = self._settings.book_operator
        answers = await asyncio.gather(
            *(
                self._call(f"/sports/{operator}/{language}/matches/{tournament}")
                for language in _LANGUAGES
            )
        )
        found: dict[str, list[dict[str, Any]]] = {}
        for records in answers:
            for record in records:
                if record["_type"] == "MATCH":
                    found.setdefault(str(record["id"]), []).append(record)

        return list(found.values())

    async def _stages(self, tournament: str) -> list[str]:
        """The child tournaments a competition splits its fixtures across."""
        records = await self._call(
            f"/sports/{self._settings.book_operator}/hu/{tournament}/{_STAGES_TOPIC}"
        )

        return [
            str(record["id"])
            for record in records
            if record["_type"] == "TOURNAMENT" and record.get("parentId") == tournament
        ]

    async def _fixtures(self, tournament: str) -> list[list[dict[str, Any]]]:
        """One tournament's fixtures, reaching into its stages when it has any."""
        found = await self._matches(tournament)

        if found:
            return found

        for stage in await self._stages(tournament):
            found += await self._matches(stage)

        return found

    async def _reindex(self) -> None:
        """Walk every tournament of every watched sport and note its events."""
        async with self._lock, self.connected():
            operator = self._settings.book_operator
            sports = [
                record["id"]
                for record in await self._call(
                    f"/sports/{operator}/hu/{_DISCIPLINES_TOPIC}"
                )
                if record["_type"] == "SPORT"
            ]
            events: list[IndexedEvent] = []
            for sport in sports:
                tournaments = [
                    record
                    for record in await self._call(
                        f"/sports/{operator}/hu/tournaments/{sport}"
                    )
                    if record["_type"] == "TOURNAMENT" and record.get("numberOfEvents")
                ]
                for tournament in tournaments:
                    events += [
                        _indexed(records)
                        for records in await self._fixtures(str(tournament["id"]))
                    ]

            self._events = events
            self._indexed_at = datetime.now(UTC)
            self._indexed.set()
            log.info("feed_indexed", events=len(events), sports=len(sports))

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

    async def resolve(self, tip: Tip) -> list[LegOffer | None]:
        """Map every leg of a tip to the offer that would be staked."""
        await self._ready()

        return [await self._resolve_leg(leg) for leg in tip.legs]

    async def market_records(self, event: IndexedEvent) -> list[dict[str, Any]]:
        """Every market, outcome and price the feed lists for one event."""
        return await self._call(
            f"/sports/{self._settings.book_operator}/hu/{event.id}/match-odds"
        )

    async def _resolve_leg(self, leg: TipLeg) -> LegOffer | None:
        """Find the one betting offer a leg names, or None if anything is unclear."""
        event = self.find_event(leg.event)
        if event is None:
            return None

        records = await self.market_records(event)

        if not records:
            log.info("event_has_no_odds", fixture=event.name)

            return None

        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            grouped.setdefault(record["_type"], []).append(record)

        named = {
            market["id"]: market
            for market in _top(
                [
                    (_market_score(leg.market, str(m["name"])), m)
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
