"""Structured logging setup."""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Configure structlog and the stdlib root logger.

    Args:
        level: Minimum level name, e.g. ``"INFO"``.
        json_output: Emit JSON lines instead of human-readable console output.
    """
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False, pad_level=False, pad_event_to=0)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
