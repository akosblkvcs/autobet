"""What a tip leg actually maps to at the bookmaker."""

import asyncio
import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any, cast
from zoneinfo import ZoneInfo

import structlog
from websockets.asyncio.client import ClientConnection, connect

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

_DISCIPLINES_TOPIC = "disciplines/NOT_LIVE/NOT_VIRTUAL/NOT_SIMULATED"
_INDEX_REFRESH_SECONDS = 6 * 60 * 60
_INDEX_WAIT_SECONDS = 150
_MIN_EVENT_SCORE = 0.85
_MIN_EVENT_GAP = 0.05

_FIXTURE_SIDES = re.compile(r" - | vs\.? ")
_DECIMAL_LINE = re.compile(r"(\d+)[.,](\d+)")
_SELECTION_KEYS = {
    "igen": "yes",
    "nem": "no",
    "yes": "yes",
    "no": "no",
    "döntetlen": "draw",
    "draw": "draw",
    "x": "draw",
}
_TRANSLITERATED = str.maketrans({"j": "i", "y": "i", "w": "v", "k": "c"})


def _fold(text: str) -> str:
    """Strip accents, case and transliteration differences."""
    stripped = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in stripped if not unicodedata.combining(c))

    return plain.casefold().translate(_TRANSLITERATED).strip()


def _normalise(name: str) -> str:
    """Reduce a market name to what the slip and the feed agree on."""
    name = name.replace("–", "-").replace("—", "-")

    return " ".join(
        _DECIMAL_LINE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", name).split()
    ).casefold()


def _initials(name: str) -> str:
    """Abbreviation hit helper."""
    return "".join(word[0] for word in _fold(name).split() if word)


def _two_sides(name: str) -> tuple[str, str] | None:
    """Split `A - B` or `A vs. B` into its two competitors, or None."""
    parts = [part.strip() for part in _FIXTURE_SIDES.split(name)]

    return (parts[0], parts[1]) if len(parts) == 2 else None  # noqa: PLR2004


def _score(query: str, candidate: str) -> float:
    """How well one side of a fixture matches one side of an event name."""
    left, right = _fold(query), _fold(candidate)

    if left == right:
        return 1.0
    if left == _initials(candidate):
        return 0.95
    if right.startswith(left) or left.startswith(right):
        return 0.9
    if left in right or right in left:
        return 0.85

    return SequenceMatcher(None, left, right).ratio()


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


class NotLoggedInError(RuntimeError):
    """The feed would not attach an account to the socket, so no bet can be placed."""


class Feed:
    """A kept-open connection to the odds feed, plus the event index."""

    def __init__(self, settings: Settings) -> None:
        """Start empty; :meth:`start` does the connecting."""
        self._settings = settings
        self._socket: ClientConnection | None = None
        self._request = 0
        self._events: list[dict[str, Any]] = []
        self._refresh: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._indexed = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Connect, start the reader, then index in the background."""
        self._socket = await connect(self._settings.book_ws, max_size=None)
        await self._socket.send(json.dumps([1, self._settings.book_realm, _WAMP_ROLES]))
        await self._socket.recv()

        self._reader = asyncio.create_task(self._read_answers(), name="feed:reader")
        self._refresh = asyncio.create_task(self._keep_fresh(), name="feed:index")

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
            "/sports#placeBetV2",
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
        """Drop the background tasks and close the socket."""
        for task in (self._refresh, self._reader):
            if task is not None:
                task.cancel()

        if self._socket is not None:
            await self._socket.close()

    @property
    def events(self) -> int:
        """How many events the index currently holds."""
        return len(self._events)

    async def _call(self, topic: str) -> list[dict[str, Any]]:
        """Ask for a topic's initial dump and return its records."""
        payload = await self._call_raw("/sports#initialDump", {"topic": topic})
        records: list[dict[str, Any]] = payload.get("records", [])

        return records

    async def _call_raw(
        self, procedure: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call one procedure and wait for its own answer."""
        assert self._socket is not None
        self._request += 1
        mine = self._request
        waiting: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting[mine] = waiting

        await self._socket.send(
            json.dumps([_WAMP_CALL, mine, {}, procedure, [], arguments])
        )
        result = await waiting
        failure = result.pop(_FAILED, None)

        if failure is None:
            return result

        log.warning("feed_call_failed", procedure=procedure, detail=failure)

        if "logged in" in failure.lower():
            raise NotLoggedInError(failure)

        return {}

    async def _read_answers(self) -> None:
        """Own ``recv`` and hand each answer to whoever asked for it."""
        assert self._socket is not None

        while True:
            message: list[Any] = json.loads(await self._socket.recv())
            # A RESULT is tagged with the request id; an ERROR repeats it one
            # place later, after the message type it is complaining about.
            if message[0] == _WAMP_RESULT:
                waiting = self._waiting.pop(message[1], None)
                if waiting is not None and not waiting.done():
                    waiting.set_result(message[4])
            elif message[0] == _WAMP_ERROR:
                waiting = self._waiting.pop(message[2], None)
                if waiting is not None and not waiting.done():
                    waiting.set_result({_FAILED: _describe(message[4:])})

    def _asleep(self) -> bool:
        """Whether we are inside the hours the tipster does not post."""
        start = self._settings.quiet_from_hour
        end = self._settings.quiet_until_hour

        if start == end:
            return False

        hour = datetime.now(ZoneInfo(self._settings.quiet_timezone)).hour

        return start <= hour < end if start < end else hour >= start or hour < end

    async def _keep_fresh(self) -> None:
        """Index now, then rebuild forever, so a tip never waits for one."""
        while True:
            if self._asleep():
                log.info("index_asleep")
            else:
                await self._reindex()

            await asyncio.sleep(_INDEX_REFRESH_SECONDS)

    async def _reindex(self) -> None:
        """Walk every tournament of every watched sport and note its events."""
        async with self._lock:
            sports = [
                record["id"]
                for record in await self._call(
                    f"/sports/{self._settings.book_operator}/hu/{_DISCIPLINES_TOPIC}"
                )
                if record["_type"] == "SPORT"
            ]
            events: list[dict[str, Any]] = []
            for sport in sports:
                tournaments = [
                    record
                    for record in await self._call(
                        f"/sports/{self._settings.book_operator}/hu/tournaments/{sport}"
                    )
                    if record["_type"] == "TOURNAMENT" and record.get("numberOfEvents")
                ]
                for tournament in tournaments:
                    events += [
                        record
                        for record in await self._call(
                            f"/sports/{self._settings.book_operator}/hu/matches/{tournament['id']}"
                        )
                        if record["_type"] == "MATCH"
                    ]

            self._events = events
            self._indexed.set()
            log.info("feed_indexed", events=len(events), sports=len(sports))

    def find_event(self, event: str) -> dict[str, Any] | None:
        """Which indexed event a tip's event line names, or None if unclear."""
        sides = _two_sides(event)
        if sides is None:
            return None

        ranked: list[tuple[float, dict[str, Any]]] = []
        for candidate in self._events:
            against = _two_sides(str(candidate.get("name") or ""))
            if against is None:
                continue
            ranked.append(
                (
                    (_score(sides[0], against[0]) + _score(sides[1], against[1])) / 2,
                    candidate,
                )
            )

        if not ranked:
            return None

        ranked.sort(key=lambda pair: pair[0], reverse=True)
        best = ranked[0]
        runner = ranked[1][0] if len(ranked) > 1 else 0.0

        if best[0] < _MIN_EVENT_SCORE or best[0] - runner < _MIN_EVENT_GAP:
            return None

        return best[1]

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

    async def _resolve_leg(self, leg: TipLeg) -> LegOffer | None:
        """Find the one betting offer a leg names, or None if anything is unclear."""
        event = self.find_event(leg.event)
        if event is None:
            return None

        records = await self._call(
            f"/sports/{self._settings.book_operator}/hu/{event['id']}/match-odds"
        )

        if not records:
            log.info("event_has_no_odds", fixture=event.get("name"))

            return None

        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            grouped.setdefault(record["_type"], []).append(record)

        wanted = _normalise(leg.market)
        markets = [
            m for m in grouped.get("MARKET", []) if _normalise(m["name"]) == wanted
        ]
        if not markets:
            return None

        market_of = {
            relation["outcomeId"]: relation["marketId"]
            for relation in grouped.get("MARKET_OUTCOME_RELATION", [])
        }
        sides = {
            event.get("homeParticipantId"): "#HOME",
            event.get("awayParticipantId"): "#AWAY",
        }
        key, code = _selection(leg, event)

        for outcome in grouped.get("OUTCOME", []):
            if market_of.get(outcome["id"]) != markets[0]["id"]:
                continue

            marked = outcome.get("code") or ""
            for participant, side in sides.items():
                if participant:
                    marked = marked.replace(f"#P{participant}", side)

            if outcome.get("headerNameKey") == key or (code and marked == code):
                offer = next(
                    (
                        o
                        for o in grouped.get("BETTING_OFFER", [])
                        if o.get("outcomeId") == outcome["id"]
                    ),
                    None,
                )
                if offer is not None:
                    return LegOffer(
                        leg=leg,
                        event_id=str(event["id"]),
                        event_name=str(event.get("name")),
                        market_id=str(markets[0]["id"]),
                        outcome_id=str(outcome["id"]),
                        betting_type_id=str(markets[0].get("bettingTypeId")),
                        offer_id=str(offer["id"]),
                        odds=float(offer["odds"]),
                        starts_at=_starts_at(event),
                    )

        return None


def _selection(leg: TipLeg, event: dict[str, Any]) -> tuple[str | None, str | None]:
    """What the leg backs, as a header key and as an outcome code."""
    folded = _fold(leg.selection)
    sides = _two_sides(str(event.get("name") or ""))
    home, away = (_fold(sides[0]), _fold(sides[1])) if sides else ("", "")

    key: str | None = _SELECTION_KEYS.get(folded)
    if key is None and folded.startswith(("több, mint", "over")):
        key = "over"
    if key is None and folded.startswith(("kevesebb, mint", "under")):
        key = "under"
    if key is None and folded and folded == home:
        key = "home"
    if key is None and folded and folded == away:
        key = "away"

    parts: list[str] = []
    for piece in re.split(r"\s*/\s*", leg.selection):
        chunk = _fold(piece)
        if chunk in ("döntetlen", "draw", "x"):
            parts.append("#D")
        elif chunk and chunk == home:
            parts.append("#HOME")
        elif chunk and chunk == away:
            parts.append("#AWAY")
        else:
            return key, None

    return key, " / ".join(parts)
