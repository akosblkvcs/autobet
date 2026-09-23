"""The dashboard: what the service is doing, for everyone signed in."""

# pyright: reportUnusedFunction=false

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from autobet.web.context import Context, templates, whoever

_COUNTED = frozenset({"tips", "bets_placed", "bets_paper", "bets_refused", "bets_failed"})


def router(context: Context) -> APIRouter:
    """Build the / route."""
    api = APIRouter()

    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        session = await whoever(request, context.store)

        if isinstance(session, Response):
            return session

        reports = context.store.reports
        latency = await reports.latency_percentiles()
        totals = await reports.totals()

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "service": context.status()
                | {"channels": list(context.telegram.channels)},
                "index": await context.index(),
                "limits": await context.limits(),
                "totals": totals,
                "archive": {
                    name: value for name, value in totals.items() if name not in _COUNTED
                }
                | {"transport_latency_ms": latency},
                "session": session,
            },
        )

    return api
