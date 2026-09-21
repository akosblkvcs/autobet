"""The archive dashboard: what the parser made of each screenshot."""

# pyright: reportUnusedFunction=false

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from autobet.web.context import Context, admin_only, templates


def router(context: Context) -> APIRouter:
    """Build the / route."""
    api = APIRouter()

    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        session = await admin_only(request, context.store)

        if isinstance(session, Response):
            return session

        reports = context.store.reports
        latency = await reports.latency_percentiles()

        response = templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "service": context.status()
                | {"channels": list(context.telegram.channels)},
                "index": await context.index(),
                "limits": await context.limits(),
                "totals": await reports.totals() | {"transport_latency_ms": latency},
                "rows": await context.store.bets.recent(50),
                "session": session,
            },
        )

        return response

    return api
