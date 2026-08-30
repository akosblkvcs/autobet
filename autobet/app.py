"""Service wiring: one event loop running the control plane and every source."""

import asyncio
import signal

import structlog
import uvicorn

from autobet.bookmakers import build_bookmaker
from autobet.config import Settings
from autobet.health import build_app
from autobet.pipeline import PipelineState, run_pipeline
from autobet.sources import build_sources
from autobet.storage import MessageStore

log = structlog.get_logger(__name__)


class _ManagedServer(uvicorn.Server):
    """A uvicorn server whose signals are owned by the service loop."""

    def install_signal_handlers(self) -> None:
        """Do nothing; :func:`run_service` handles SIGINT/SIGTERM centrally."""


async def run_service(settings: Settings) -> None:
    """Run every configured source, the bet placer and the control plane."""
    store = await MessageStore.connect(settings.database_url)
    state = PipelineState()

    sources = build_sources(settings)
    bookmaker = build_bookmaker(settings)

    for source in sources:
        await source.start()

    await bookmaker.start()

    log.info(
        "service_configured",
        sources=[source.name for source in sources],
        bookmaker=bookmaker.name,
        dry_run=settings.dry_run,
    )

    server = _ManagedServer(
        uvicorn.Config(
            build_app(settings, store, state, sources),
            host=settings.http_host,
            port=settings.http_port,
            log_config=None,
            access_log=False,
        )
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    http = asyncio.create_task(server.serve(), name="http")
    pipelines = [
        asyncio.create_task(
            run_pipeline(
                source.messages(),
                store,
                state,
                bookmaker,
            ),
            name=f"pipeline:{source.name}",
        )
        for source in sources
    ]

    for task in [http, *pipelines]:
        task.add_done_callback(lambda _: stop.set())

    log.info("service_started", http=f"http://{settings.http_host}:{settings.http_port}")
    await stop.wait()

    log.info("service_stopping")
    server.should_exit = True

    for task in pipelines:
        task.cancel()

    await asyncio.gather(http, *pipelines, return_exceptions=True)
    await bookmaker.stop()

    for source in sources:
        await source.stop()

    await store.close()
    log.info("service_stopped", processed=state.processed)
