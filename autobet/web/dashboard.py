"""The archive dashboard: what the parser made of each screenshot."""

# pyright: reportUnusedFunction=false

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from autobet.web.auth import TOKEN_COOKIE, TOKEN_COOKIE_MAX_AGE, presented_token
from autobet.web.context import Context, templates


def router(context: Context) -> APIRouter:
    """Build the / route."""
    api = APIRouter()

    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        token = presented_token(request, context.settings)

        if token is None:
            return templates.TemplateResponse(
                request,
                "forbidden.html",
                {"token_configured": bool(context.settings.autobet_token)},
                status_code=403,
            )

        response = templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "service": context.status()
                | {"channels": list(context.telegram.channels)},
                "feed": context.feed(),
                "limits": context.limits(),
                "totals": await context.store.totals()
                | {"transport_latency_ms": await context.store.latency_percentiles()},
                "rows": await context.store.recent_tips(50),
            },
        )
        response.set_cookie(
            TOKEN_COOKIE,
            token,
            max_age=TOKEN_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=context.settings.environment == "production",
        )

        return response

    return api
