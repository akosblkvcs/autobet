"""What every page needs to render, shared by the routers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import humanize
from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from autobet.config import Settings
from autobet.models import SignedIn, utcnow
from autobet.pipeline import PipelineState
from autobet.storage import Store
from autobet.telegram import Telegram
from autobet.web.auth import Provider, signed_in

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def whoever(request: Request, store: Store) -> SignedIn | Response:
    """Whoever is signed in, or the redirect that asks them to be."""
    session = await signed_in(request, store)

    return session or RedirectResponse("/auth/login", status_code=303)


async def admin_only(request: Request, store: Store) -> SignedIn | Response:
    """The signed-in admin, or the response to send instead of the page."""
    session = await signed_in(request, store)

    if session is None:
        return RedirectResponse("/auth/login", status_code=303)

    if not session.user.is_admin:
        return templates.TemplateResponse(
            request,
            "denied.html",
            {"reason": "this page is for administrators", "session": session},
            status_code=403,
        )

    return session


@dataclass(frozen=True, slots=True)
class Context:
    """Everything the pages read, built once and closed over by each router."""

    settings: Settings
    store: Store
    state: PipelineState
    telegram: Telegram
    provider: Provider

    async def limits(self) -> dict[str, Any]:
        """The stored limits that decide whether a tip becomes a bet."""
        policy = await self.store.config.policy()

        return {
            "paper_mode": policy.paper_mode,
            "stake": str(policy.stake),
            "max_odds_drop": f"{policy.max_odds_drop_percent:g}%",
            "max_odds_rise": f"{policy.max_odds_rise_percent:g}%",
        }

    async def index(self) -> dict[str, Any]:
        """What the stored event index holds and how stale it is."""
        held = await self.store.events.summary()
        built, next_off = held["built"], held["next_off"]

        return {
            "events": held["events"],
            "upcoming": held["upcoming"],
            "sports": held["sports"],
            "tournaments": held["tournaments"],
            "priced": f"{held['priced']} ({held['markets']} markets)",
            "next_off": (
                "nothing ahead"
                if next_off is None
                else f"in {humanize.naturaldelta(next_off - utcnow())}"
            ),
            "built": (
                f"{humanize.naturaldelta(utcnow() - built)} ago" if built else "not yet"
            ),
        }

    def status(self) -> dict[str, Any]:
        """This process only, read from memory so the probe never touches Postgres."""
        idle = self.state.seconds_since_last_message()

        return {
            "connected": self.telegram.healthy(),
            "last_message": (
                "none yet" if idle is None else f"{humanize.naturaldelta(idle)} ago"
            ),
            "messages_seen": self.state.processed,
            "tips_parsed": self.state.tips,
            "uptime": humanize.naturaldelta(self.state.uptime_seconds),
        }
