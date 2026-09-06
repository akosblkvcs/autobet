"""Mint a betting session from the book's login, over plain HTTP."""

from typing import Any

import httpx2
import structlog

from autobet.config import Settings

log = structlog.get_logger(__name__)

_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_TIMEOUT = 30.0
_OK = 200


class SessionError(RuntimeError):
    """No betting session could be minted. Carries the reason and the fix."""

    def __init__(self, reason: str, fix: str) -> None:
        """Store the fix alongside the reason; the caller decides how to show it."""
        super().__init__(reason)
        self.fix = fix


async def mint_ce_session(settings: Settings) -> str:
    """Log in and return the token the feed needs to place bets."""
    if not (settings.book_username and settings.book_api):
        raise SessionError("no book login configured", "set BOOK_USERNAME and BOOK_API")

    headers = {
        "User-Agent": _AGENT,
        "Accept": "application/json",
        "Origin": settings.book_site,
        "Referer": f"{settings.book_site}/",
    }

    async with httpx2.AsyncClient(
        headers=headers, timeout=_TIMEOUT, follow_redirects=True
    ) as client:
        signed_in = await client.post(
            f"{settings.book_api}/v1/player/legislation/login?language=hu",
            json={
                "username": settings.book_username,
                "password": settings.book_password.get_secret_value(),
            },
        )
        if signed_in.status_code != _OK:
            raise SessionError(
                f"login refused with {signed_in.status_code}",
                "check BOOK_USERNAME and BOOK_PASSWORD",
            )

        opened: dict[str, Any] = signed_in.json()
        client.headers["X-SessionId"] = str(opened.get("sessionId") or "")

        player: dict[str, Any] = (
            await client.get(f"{settings.book_api}/v1/player/session/player?language=hu")
        ).json()
        sid = str(player.get("Guid") or "")
        if not sid:
            raise SessionError("no session guid after login", "check the credentials")

        loader: dict[str, Any] = (
            await client.get(
                settings.book_loader, params={"_sid": sid, "launchApi": "true"}
            )
        ).json()

    ce_session = str(loader.get("ceSession") or "")
    if not ce_session:
        raise SessionError(
            f"loader gave no ceSession: {loader.get('ErrorMessage')}",
            "the login worked but the sportsbook refused it",
        )

    log.info("betting_session_minted", username=player.get("Username"))

    return ce_session
