"""What a tip leg actually maps to at the bookmaker."""

import asyncio
import json
import re
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Any

import structlog
from websockets.asyncio.client import ClientConnection, connect

from autobet.models import LegOffer, Tip, TipLeg

log = structlog.get_logger(__name__)

_WAMP_URL = "wss://sportsapi.tippmixpro.hu/v2"
_WAMP_REALM = "www.tippmixpro.hu"
_OPERATOR_ID = "2901"
_WAMP_HELLO: list[Any] = [
    1,
    _WAMP_REALM,
    {"agent": "autobet", "roles": {"caller": {}, "subscriber": {}}},
]
_WAMP_CALL = 48
_WAMP_RESULT = 50
_WAMP_ERROR = 8

_DISCIPLINES_TOPIC = "disciplines/NOT_LIVE/NOT_VIRTUAL/NOT_SIMULATED"
_INDEX_REFRESH_SECONDS = 900
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


def _fold(text: str) -> str:
    """Strip accents and case."""
    stripped = unicodedata.normalize("NFKD", text)

    return "".join(c for c in stripped if not unicodedata.combining(c)).casefold().strip()


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


class Feed:
    """A kept-open connection to the odds feed, plus the event index."""

    def __init__(self) -> None:
        """Start empty; :meth:`start` does the connecting."""
        self._socket: ClientConnection | None = None
        self._request = 0
        self._events: list[dict[str, Any]] = []
        self._refresh: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Connect, then index in the background."""
        self._socket = await connect(_WAMP_URL, max_size=None)
        await self._socket.send(json.dumps(_WAMP_HELLO))
        await self._socket.recv()

        self._refresh = asyncio.create_task(self._keep_fresh(), name="feed:index")

    async def authenticate(self, ce_session: str) -> None:
        """Attach an account to this socket, so bets may be placed on it."""
        await self._call_raw(
            "/sports#loginWithCeSession", {"lang": "hu", "ceSession": ce_session}
        )

        log.info("feed_authenticated")

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
                "type": "SINGLE" if len(offers) == 1 else "COMBI",
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
        """Drop the refresh task and close the socket."""
        if self._refresh is not None:
            self._refresh.cancel()

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
        """Call one procedure and return its result, or {} if the feed refused."""
        assert self._socket is not None
        self._request += 1
        mine = self._request
        await self._socket.send(
            json.dumps([_WAMP_CALL, mine, {}, procedure, [], arguments])
        )

        while True:
            message: list[Any] = json.loads(await self._socket.recv())

            if message[0] == _WAMP_RESULT and message[1] == mine:
                result: dict[str, Any] = message[4]

                return result
            if message[0] == _WAMP_ERROR and message[2] == mine:
                log.warning("feed_call_failed", procedure=procedure, detail=message[4:])

                return {}

    async def _keep_fresh(self) -> None:
        """Index now, then rebuild forever, so a tip never waits for one."""
        while True:
            await self._reindex()
            await asyncio.sleep(_INDEX_REFRESH_SECONDS)

    async def _reindex(self) -> None:
        """Walk every tournament of every watched sport and note its events."""
        async with self._lock:
            sports = [
                record["id"]
                for record in await self._call(
                    f"/sports/{_OPERATOR_ID}/hu/{_DISCIPLINES_TOPIC}"
                )
                if record["_type"] == "SPORT"
            ]
            events: list[dict[str, Any]] = []
            for sport in sports:
                tournaments = [
                    record
                    for record in await self._call(
                        f"/sports/{_OPERATOR_ID}/hu/tournaments/{sport}"
                    )
                    if record["_type"] == "TOURNAMENT" and record.get("numberOfEvents")
                ]
                for tournament in tournaments:
                    events += [
                        record
                        for record in await self._call(
                            f"/sports/{_OPERATOR_ID}/hu/matches/{tournament['id']}"
                        )
                        if record["_type"] == "MATCH"
                    ]

            self._events = events
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

    async def resolve(self, tip: Tip) -> list[LegOffer | None]:
        """Map every leg of a tip to the offer that would be staked."""
        return [await self._resolve_leg(leg) for leg in tip.legs]

    async def _resolve_leg(self, leg: TipLeg) -> LegOffer | None:
        """Find the one betting offer a leg names, or None if anything is unclear."""
        event = self.find_event(leg.event)
        if event is None:
            return None

        records = await self._call(f"/sports/{_OPERATOR_ID}/hu/{event['id']}/match-odds")
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
