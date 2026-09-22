"""The ingestion loop: message in, archive, parse, bet."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from autobet.bookmaker import Bookmaker
from autobet.models import BetResult, IncomingMessage, Placement, Tip, utcnow
from autobet.storage import Store

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class PipelineState:
    """Live counters describing what the ingestion loop has done so far."""

    started_at: datetime = field(default_factory=utcnow)
    last_message_at: datetime | None = None
    processed: int = 0
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
    store: Store,
    state: PipelineState,
    bookmaker: Bookmaker,
    parse: Callable[[IncomingMessage], Awaitable[Tip | None]],
) -> None:
    """Consume one message stream: archive everything, bet on what parses.

    Args:
        messages: Stream of messages from Telegram.
        store: The archive every message is written to.
        state: Counters updated in place, read by the health endpoint.
        bookmaker: Where a parsed tip gets staked.
        parse: Reads a message and stakes it, returning None if it is not a tip.
    """
    async for message in messages:
        is_new = await store.archive.add(message)
        state.last_message_at = message.received_at
        state.processed += 1

        log.info(
            "message_archived" if is_new else "message_duplicate",
            external_id=message.external_id,
            channel=message.channel,
            latency_ms=message.transport_latency_ms,
        )

        if not is_new:
            continue

        if message.sent_at < state.started_at:
            log.info(
                "message_stale",
                external_id=message.external_id,
                sent_at=message.sent_at.isoformat(),
            )

            continue

        tip: Tip | None = None
        recorded = False

        try:
            policy = await store.config.policy()
            tip = await parse(message)

            if tip is None:
                continue

            state.tips += 1

            placement = await bookmaker.place(tip, policy)

            await store.bets.record(tip, placement)
            recorded = True

            for result in placement.results:
                log.info(
                    f"bet_{result.state}",
                    user=result.user_id,
                    legs=len(tip.legs),
                    odds=None if tip.odds is None else round(tip.odds, 3),
                    stake=result.stake,
                    reference=result.reference,
                    refusal=None if result.refusal is None else result.refusal.code,
                    error=result.error or None,
                    latency_ms=result.total_latency_ms,
                )
        except Exception as error:
            log.exception("tip_failed", external_id=message.external_id)

            if tip is not None and not recorded:
                await store.bets.record(
                    tip,
                    Placement(
                        resolutions=(),
                        results=(
                            BetResult(
                                tip=tip,
                                reference="",
                                placed_at=utcnow(),
                                error=type(error).__name__,
                            ),
                        ),
                    ),
                )
