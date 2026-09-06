"""Mint a betting session from the browser profile a human logged in with."""

import asyncio
import json
from typing import Any

import structlog
from playwright.async_api import Response, async_playwright

from autobet.config import Settings

log = structlog.get_logger(__name__)

_PAGE = "https://www.tippmixpro.hu/hu/fogadas/i"
_LOADER = "gamelaunch-hu.everymatrix.com/Loader/StartInfo"
_TIMEOUT_MS = 60_000


class SessionError(RuntimeError):
    """No betting session could be minted. Carries the reason and the fix."""

    def __init__(self, reason: str, fix: str) -> None:
        """Store the fix alongside the reason; the caller decides how to show it."""
        super().__init__(reason)
        self.fix = fix


async def mint_ce_session(settings: Settings) -> str:
    """Open the site with the stored login and return the token bets need."""
    found: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def watch(response: Response) -> None:
        if _LOADER not in response.url or found.done():
            return

        body: dict[str, Any] = json.loads(await response.text())
        if body.get("ceSession"):
            found.set_result(str(body["ceSession"]))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch_persistent_context(
            str(settings.browser_profile),
            headless=True,
            locale="hu-HU",
            args=["--password-store=basic", "--no-first-run"],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        page.on("response", lambda response: asyncio.create_task(watch(response)))

        await page.goto(_PAGE, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
        try:
            async with asyncio.timeout(_TIMEOUT_MS / 1000):
                ce_session = await found
        except TimeoutError:
            raise SessionError(
                "no ceSession from the loader",
                "log in again with data/feed/capture_betslip.py",
            ) from None
        finally:
            await browser.close()

    log.info("betting_session_minted")

    return ce_session
