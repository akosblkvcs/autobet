"""The HTTP control plane: one FastAPI app, one router per page."""

from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from autobet.config import Settings
from autobet.pipeline import PipelineState
from autobet.sources import TipSource
from autobet.storage import MessageStore
from autobet.web import dashboard, health
from autobet.web.context import Context


def build_app(
    settings: Settings,
    store: MessageStore,
    state: PipelineState,
    sources: Sequence[TipSource],
) -> FastAPI:
    """Create the control-plane app with every page mounted.

    Args:
        settings: Runtime configuration.
        store: The message archive, for read-only views.
        state: Live pipeline counters.
        sources: The running tip sources, asked whether they are still live.

    Returns:
        A FastAPI application.
    """
    app = FastAPI(title="autobet", docs_url=None, redoc_url=None)
    context = Context(settings=settings, store=store, state=state, sources=sources)

    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.include_router(health.router(context))
    app.include_router(dashboard.router(context))

    return app
