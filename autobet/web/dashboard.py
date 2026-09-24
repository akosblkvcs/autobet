"""The dashboard and the tip list: the two pages every signed-in person sees."""

# pyright: reportUnusedFunction=false

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from autobet.web.context import Context, templates, whoever


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

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "service": context.status()
                | {"channels": list(context.telegram.channels)},
                "index": await context.index(),
                "limits": await context.limits(),
                "totals": await reports.totals() | {"transport_latency_ms": latency},
                "session": session,
            },
        )

    @api.get("/tips", response_class=HTMLResponse)
    async def tips(request: Request) -> Response:
        session = await whoever(request, context.store)

        if isinstance(session, Response):
            return session

        return templates.TemplateResponse(
            request,
            "tips.html",
            {"rows": await context.store.bets.recent(50), "session": session},
        )

    @api.post("/tips/sync")
    async def sync_settlements(request: Request) -> Response:
        session = await whoever(request, context.store)

        if isinstance(session, Response):
            return session

        await context.bookmaker.settle_bets()

        return RedirectResponse("/tips", status_code=303)

    return api
