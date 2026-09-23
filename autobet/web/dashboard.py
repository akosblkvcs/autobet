"""The dashboard: what the service is doing, for everyone signed in."""

# pyright: reportUnusedFunction=false

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

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

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "service": context.service(),
                "index": await context.index(),
                "limits": await context.limits(session.user.id),
                "totals": await reports.totals(session.user.id),
                "latency": await reports.latency(),
                "session": session,
            },
        )

    return api
