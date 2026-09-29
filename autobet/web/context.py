"""What every page needs to render, shared by the routers."""

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import humanize
from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from autobet.config import Settings
from autobet.models import BetLeg, SignedIn, utcnow
from autobet.pipeline import PipelineState
from autobet.storage import Store
from autobet.telegram import Telegram
from autobet.web.auth import Provider, signed_in

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_ZONE = ZoneInfo("Europe/Budapest")
_PAGE = 10


@dataclass(frozen=True, slots=True)
class Flag:
    """A yes/no on a card, coloured by what it means rather than whether it is true."""

    text: str
    tone: str


def _flag(yes: bool, when_yes: str, when_no: str) -> Flag:
    """The yes/no, and the colour each side of it carries."""
    return Flag("yes", when_yes) if yes else Flag("no", when_no)


async def bets_page(
    store: Store, page: int, user_id: int | None = None
) -> dict[str, Any]:
    """One page of tips that reached a verdict, and whether an older page follows."""
    rows = await store.bets.recent(_PAGE + 1, user_id, offset=(page - 1) * _PAGE)

    return {"rows": rows[:_PAGE], "page": page, "more": len(rows) > _PAGE}


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

    async def limits(self, user_id: int) -> dict[str, Any]:
        """What this person's bets are judged by: their own terms over the service's."""
        policy = await self.store.config.policy()
        users = self.store.users
        terms = (await users.policy(user_id)).over(policy)
        own = await users.stored_policy(user_id)

        def theirs(name: str, shown: str) -> str:
            """Say whose number this is, so an inherited one is not read as set."""
            return f"{shown}" if name in own else shown

        return {
            "paper_mode": _flag(terms.paper_mode, "calm", "plain"),
            "stake": theirs("stake", str(terms.stake)),
            "max_odds_drop": theirs(
                "max_odds_drop_percent", f"{terms.max_odds_drop_percent:g}%"
            ),
            "max_odds_rise": theirs(
                "max_odds_rise_percent", f"{terms.max_odds_rise_percent:g}%"
            ),
        }

    async def schedule(self, user_id: int) -> dict[str, list[BetLeg]]:
        """This person's bets by the local day their fixture kicks off."""
        today = datetime.now(_ZONE).date()
        since = datetime.combine(today - timedelta(days=1), time(), _ZONE)
        days: dict[str, list[BetLeg]] = {"yesterday": [], "today": [], "upcoming": []}

        for leg in await self.store.bets.standing(user_id, since):
            local = replace(leg, starts_at=leg.starts_at.astimezone(_ZONE))
            day = local.starts_at.date()
            key = "yesterday" if day < today else "today" if day == today else "upcoming"
            days[key].append(local)

        return days

    async def index(self) -> dict[str, Any]:
        """What the stored event index holds and how stale it is."""
        held = await self.store.events.summary()
        built = held["built"]

        return {
            "events": held["events"],
            "upcoming": f"{held['upcoming']} of {held['declared']} declared",
            "sports": held["sports"],
            "tournaments": held["tournaments"],
            "markets": f"{held['markets']} across {held['priced']} fixtures",
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

    def service(self) -> dict[str, Any]:
        """The Telegram card: the probe's status, coloured and with the chats."""
        return self.status() | {
            "connected": _flag(self.telegram.healthy(), "good", "bad"),
            "channels": list(self.telegram.channels),
        }
