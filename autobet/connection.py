"""One socket to the bookmaker's feed, open for one piece of work."""

import contextlib
import json
from collections.abc import AsyncGenerator, Sequence
from typing import Any, cast

import structlog
from websockets.asyncio.client import ClientConnection, connect

from autobet.config import Settings
from autobet.models import LegOffer

log = structlog.get_logger(__name__)

_WAMP_ROLES: dict[str, Any] = {
    "agent": "autobet",
    "roles": {"caller": {}, "subscriber": {}},
}
_WAMP_HELLO = 1
_WAMP_CALL = 48
_WAMP_RESULT = 50


class NotLoggedInError(RuntimeError):
    """The feed would not attach an account to the socket, so no bet can be placed."""


class FeedCallError(RuntimeError):
    """The feed answered a call with an error, so it says nothing about the board."""


def _describe(parts: list[Any]) -> str:
    """The human-readable half of a WAMP error frame, if it carries one."""
    for part in parts:
        if isinstance(part, dict):
            described = cast("dict[str, Any]", part)
            if described.get("desc"):
                return str(described["desc"])

    return str(parts)


class Connection:
    """One socket to the feed, open for one piece of work and closed with it."""

    def __init__(self, socket: ClientConnection, operator: str) -> None:
        """Wrap a socket that has already been greeted."""
        self._socket = socket
        self._operator = operator
        self._request = 0

    async def call(self, procedure: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one procedure and return its answer.

        One call is made at a time, so the next frame to arrive answers this one.
        """
        self._request += 1

        await self._socket.send(
            json.dumps([_WAMP_CALL, self._request, {}, procedure, [], arguments])
        )

        frame: list[Any] = json.loads(await self._socket.recv())

        if frame[0] == _WAMP_RESULT:
            answer: dict[str, Any] = frame[4]

            return answer

        failure = _describe(frame[4:])

        if "logged in" in failure.lower():
            raise NotLoggedInError(failure)

        raise FeedCallError(f"{procedure}: {failure}")

    async def dump(self, resource: str, language: str = "hu") -> list[dict[str, Any]]:
        """Every record the feed holds for one resource, such as `tournaments/1`."""
        payload = await self.call(
            "/sports#initialDump",
            {"topic": f"/sports/{self._operator}/{language}/{resource}"},
        )
        records: list[dict[str, Any]] = payload.get("records", [])

        return records

    async def match_odds(self, event_id: str) -> list[dict[str, Any]]:
        """Every market, outcome and price the feed lists for one event."""
        return await self.dump(f"{event_id}/match-odds")

    async def prices(self, offer_ids: Sequence[str]) -> dict[str, float]:
        """What the book charges for offers it has already quoted, right now."""
        records = await self.dump(f"bettingOffers/{','.join(offer_ids)}")

        return {
            str(record["id"]): float(record["odds"])
            for record in records
            if record["_type"] == "BETTING_OFFER" and record.get("odds")
        }

    async def authenticate(self, ce_session: str) -> str:
        """Attach an account to this socket, so bets may be placed on it.

        Returns:
            The username the feed says we are.

        Raises:
            NotLoggedInError: The feed refused the token.
        """
        answer = await self.call(
            "/sports#loginWithCeSession", {"lang": "hu", "ceSession": ce_session}
        )
        username = str(answer.get("username") or "")

        if not username:
            raise NotLoggedInError(str(answer.get("message") or "no answer"))

        log.info("feed_authenticated", username=username)

        return username

    async def place_bet(self, offers: Sequence[LegOffer], stake: float) -> dict[str, Any]:
        """Place one bet covering every leg, and return whatever the feed says."""
        return await self.call(
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


@contextlib.asynccontextmanager
async def connected(settings: Settings) -> AsyncGenerator[Connection]:
    """Open a socket for one piece of work, and close it when the work ends."""
    async with connect(settings.book_ws, max_size=None) as socket:
        await socket.send(json.dumps([_WAMP_HELLO, settings.book_realm, _WAMP_ROLES]))
        await socket.recv()

        log.info("feed_connected")

        try:
            yield Connection(socket, settings.book_operator)
        finally:
            log.info("feed_closed")
