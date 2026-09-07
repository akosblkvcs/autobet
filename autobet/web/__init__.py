"""The HTTP control plane: one FastAPI app, one router per page."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from autobet.bookmaker import Bookmaker
from autobet.config import Settings
from autobet.pipeline import PipelineState
from autobet.storage import MessageStore
from autobet.telegram import TelegramSource
from autobet.web import dashboard, health
from autobet.web.context import Context


def build_app(
    settings: Settings,
    store: MessageStore,
    state: PipelineState,
    source: TelegramSource,
    bookmaker: Bookmaker,
) -> FastAPI:
    """Create the control-plane app with every page mounted.

    Args:
        settings: Runtime configuration.
        store: The message archive, for read-only views.
        state: Live pipeline counters.
        source: The running Telegram source, asked whether it is still live.
        bookmaker: Asked how fresh its event index is.

    Returns:
        A FastAPI application.
    """
    app = FastAPI(title="autobet", docs_url=None, redoc_url=None)
    context = Context(
        settings=settings,
        store=store,
        state=state,
        source=source,
        bookmaker=bookmaker,
    )

    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.include_router(health.router(context))
    app.include_router(dashboard.router(context))

    return app
