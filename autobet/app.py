"""Service wiring: one event loop running the control plane and every source."""

import asyncio
import signal
from functools import partial

import structlog
import uvicorn

from autobet.bookmakers import build_bookmaker
from autobet.config import Settings
from autobet.parser import build_claude, parse_tip
from autobet.pipeline import PipelineState, run_pipeline
from autobet.sources import build_sources
from autobet.storage import MessageStore
from autobet.web import build_app

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
    claude = build_claude(settings)
    parse = partial(parse_tip, claude=claude, stake=settings.stake)

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
                parse,
            ),
            name=f"pipeline:{source.name}",
        )
        for source in sources
    ]

    def report(task: asyncio.Task[None]) -> None:
        """Say why a task ended, since any one of them ending stops the service."""
        if not task.cancelled() and task.exception() is not None:
            log.error("task_failed", task=task.get_name(), exc_info=task.exception())

        stop.set()

    for task in [http, *pipelines]:
        task.add_done_callback(report)

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

    await claude.close()
    await store.close()
    log.info("service_stopped", processed=state.processed)
