"""The HTTP control plane: one FastAPI app, one router per page."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from autobet.config import Settings
from autobet.pipeline import PipelineState
from autobet.storage import Store
from autobet.telegram import Telegram
from autobet.web import admin, dashboard, events, health, signin, user
from autobet.web.auth import Provider
from autobet.web.context import Context


def build_app(
    settings: Settings,
    store: Store,
    state: PipelineState,
    telegram: Telegram,
) -> FastAPI:
    """Create the control-plane app with every page mounted."""
    app = FastAPI(title="autobet", docs_url=None, redoc_url=None)
    context = Context(
        settings=settings,
        store=store,
        state=state,
        telegram=telegram,
        provider=Provider(settings),
    )

    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.include_router(health.router(context))
    app.include_router(signin.router(context))
    app.include_router(dashboard.router(context))
    app.include_router(user.router(context))
    app.include_router(events.router(context))
    app.include_router(admin.router(context))

    return app
