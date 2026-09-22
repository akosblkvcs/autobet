"""The index as a page: is this fixture on the board, and how fresh is that."""

# pyright: reportUnusedFunction=false

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from autobet.web.context import Context, templates, whoever


def router(context: Context) -> APIRouter:
    """Build the /events route."""
    api = APIRouter()

    @api.get("/events", response_class=HTMLResponse)
    async def events(request: Request, q: str = "") -> Response:
        session = await whoever(request, context.store)

        if isinstance(session, Response):
            return session

        found = await context.store.events.search(q) if q.strip() else []

        return templates.TemplateResponse(
            request,
            "events.html",
            {
                "query": q,
                "found": found,
                "index": await context.index(),
                "session": session,
            },
        )

    return api
