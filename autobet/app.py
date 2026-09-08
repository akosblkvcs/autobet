"""Service wiring: one event loop running the control plane and the pipeline."""

import asyncio
import signal
from functools import partial

import structlog
import uvicorn

from autobet.bookmaker import Bookmaker
from autobet.config import Settings
from autobet.parser import build_claude, build_text_prompt, parse_tip
from autobet.pipeline import PipelineState, run_pipeline
from autobet.storage import MessageStore
from autobet.telegram import TelegramSource
from autobet.web import build_app

log = structlog.get_logger(__name__)


class _ManagedServer(uvicorn.Server):
    """A uvicorn server whose signals are owned by the service loop."""

    def install_signal_handlers(self) -> None:
        """Do nothing; :func:`run_service` handles SIGINT/SIGTERM centrally."""


async def run_service(settings: Settings) -> None:
    """Run the Telegram source, the bet placer and the control plane."""
    store = await MessageStore.connect(settings.database_url)
    state = PipelineState()

    source = TelegramSource(settings)
    bookmaker = Bookmaker(settings)
    claude = build_claude(settings)
    parse = partial(
        parse_tip,
        claude=claude,
        stake=settings.stake,
        text_prompt=build_text_prompt(),
    )

    await source.start()
    await bookmaker.start()

    log.info(
        "service_configured",
        channels=source.channels,
        dry_run=settings.dry_run,
    )

    server = _ManagedServer(
        uvicorn.Config(
            build_app(settings, store, state, source, bookmaker),
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
    pipeline = asyncio.create_task(
        run_pipeline(source.messages(), store, state, bookmaker, parse),
        name="pipeline",
    )

    def report(task: asyncio.Task[None]) -> None:
        """Say why a task ended, since any one of them ending stops the service."""
        if not task.cancelled() and task.exception() is not None:
            log.error("task_failed", task=task.get_name(), exc_info=task.exception())

        stop.set()

    for task in (http, pipeline):
        task.add_done_callback(report)

    log.info("service_started", http=f"http://{settings.http_host}:{settings.http_port}")

    await stop.wait()

    log.info("service_stopping")

    server.should_exit = True

    pipeline.cancel()

    await asyncio.gather(http, pipeline, return_exceptions=True)
    await bookmaker.stop()
    await source.stop()

    await claude.close()
    await store.close()

    log.info("service_stopped", processed=state.processed)
