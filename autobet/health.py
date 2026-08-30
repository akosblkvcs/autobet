"""HTTP control plane: health checks and a minimal archive viewer."""

# pyright: reportUnusedFunction=false

import html
import secrets
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from autobet.config import Settings
from autobet.pipeline import PipelineState
from autobet.sources import TipSource
from autobet.storage import MessageStore

# Set once the token has been presented, so it is pasted once per browser.
_COOKIE = "autobet_token"
_STYLE = (
    "<style>body{font:14px/1.5 ui-monospace,monospace;margin:2rem;"
    "max-width:70rem}dl{display:grid;grid-template-columns:auto 1fr;"
    "gap:.1rem 1rem;margin:0 0 2rem}"
    "dt{color:#666}table{border-collapse:collapse;width:100%}"
    "td{border-top:1px solid #ddd;padding:.4rem;vertical-align:top}"
    "pre{margin:0;white-space:pre-wrap;font:inherit}</style>"
)


def build_app(
    settings: Settings,
    store: MessageStore,
    state: PipelineState,
    sources: Sequence[TipSource],
) -> FastAPI:
    """Create the control-plane app.

    Args:
        settings: Runtime configuration.
        store: The message archive, for read-only views.
        state: Live pipeline counters.
        sources: The running tip sources, asked whether they are still live.

    Returns:
        A FastAPI application.
    """
    app = FastAPI(title="autobet", docs_url=None, redoc_url=None)

    def status() -> dict[str, Any]:
        """Live counters, read from memory so the probe never touches Postgres."""
        idle = state.seconds_since_last_message()
        return {
            "sources": {source.name: source.healthy() for source in sources},
            "dry_run": settings.dry_run,
            "uptime_seconds": round(state.uptime_seconds, 1),
            "processed": state.processed,
            "duplicates": state.duplicates,
            "tips": state.tips,
            "seconds_since_last_message": None if idle is None else round(idle, 1),
        }

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        body = status()
        ok = all(body["sources"].values())
        return JSONResponse(body, status_code=200 if ok else 503)

    def presented_token(request: Request) -> str | None:
        """Return the caller's token when it matches the configured one."""
        given = request.cookies.get(_COOKIE) or request.query_params.get("token")
        expected = settings.autobet_token

        if not expected or not given:
            return None

        return given if secrets.compare_digest(given, expected) else None

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        token = presented_token(request)
        if token is None:
            reason = (
                "AUTOBET_TOKEN is not set on the server."
                if not settings.autobet_token
                else "Forbidden"
            )
            return HTMLResponse(
                f"<!doctype html><meta charset=utf-8><title>autobet</title>{_STYLE}"
                f"<h1>403</h1><p>{reason}</p>",
                status_code=403,
            )

        body = status() | {
            "archived": await store.count(),
            "transport_latency_ms": await store.latency_percentiles(),
        }
        stats = "".join(
            f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>"
            for k, v in body.items()
        )
        rows = "".join(
            f"<tr><td>{m.received_at:%m-%d %H:%M:%S}</td>"
            f"<td>{m.transport_latency_ms}&nbsp;ms</td>"
            f"<td>{html.escape(m.source)}</td>"
            f"<td>{html.escape(m.channel)}</td>"
            f"<td><pre>{html.escape(m.text[:600])}</pre></td></tr>"
            for m in await store.recent(50)
        )

        response = HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>autobet</title>"
            f"{_STYLE}<h1>autobet</h1><dl>{stats}</dl>"
            f"<h2>Recent messages</h2><table>{rows}</table>"
        )
        response.set_cookie(
            _COOKIE,
            token,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            # Only over TLS in prod; local dev is plain http on loopback.
            secure=settings.environment == "production",
        )
        return response

    return app
