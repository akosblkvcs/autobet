"""The ingestion loop: message in, archive, parse, bet."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from autobet.bookmakers import Bookmaker
from autobet.models import IncomingMessage, Tip, utcnow
from autobet.storage import MessageStore

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class PipelineState:
    """Live counters describing what the ingestion loop has done so far."""

    started_at: datetime = field(default_factory=utcnow)
    last_message_at: datetime | None = None
    processed: int = 0
    duplicates: int = 0
    tips: int = 0

    @property
    def uptime_seconds(self) -> float:
        """Seconds since the loop started."""
        return (utcnow() - self.started_at).total_seconds()

    def seconds_since_last_message(self) -> float | None:
        """Seconds since the most recent message, or None if there was none."""
        if self.last_message_at is None:
            return None

        return (utcnow() - self.last_message_at).total_seconds()


async def run_pipeline(
    messages: AsyncIterator[IncomingMessage],
    store: MessageStore,
    state: PipelineState,
    bookmaker: Bookmaker,
    parse: Callable[[IncomingMessage], Awaitable[Tip | None]],
) -> None:
    """Consume one message stream: archive everything, bet on what parses.

    Args:
        messages: Stream from a single tip source.
        store: The archive every message is written to.
        state: Counters updated in place, read by the health endpoint.
        bookmaker: Where a parsed tip gets staked.
        parse: Reads a message and returns a tip, or None if it is not one.
    """
    async for message in messages:
        is_new = await store.add(message)
        state.last_message_at = message.received_at
        state.processed += 1

        if not is_new:
            state.duplicates += 1

        log.info(
            "message_archived" if is_new else "message_duplicate",
            external_id=message.external_id,
            channel=message.channel,
            latency_ms=message.transport_latency_ms,
        )

        if not is_new:
            continue

        tip = await parse(message)

        if tip is None:
            continue

        state.tips += 1

        result = await bookmaker.place(tip)

        await store.set_tip(tip)
        log.info(
            "bet_placed" if result.accepted else "bet_rejected",
            bookmaker=bookmaker.name,
            legs=len(tip.legs),
            odds=round(tip.odds, 3),
            stake=tip.stake,
            reference=result.reference,
            latency_ms=result.total_latency_ms,
        )
