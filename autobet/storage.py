"""Postgres archive of every message we observed."""

import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from asyncpg import Pool, Record, create_pool

from autobet.migrate import apply_migrations
from autobet.models import IncomingMessage, LegOffer, MessageWithTip, Tip, TipLeg

_MESSAGE_COLUMNS = (
    "external_id, channel, sent_at, received_at, text, media_kind, media_path"
)
_TRANSPORT_LATENCY_MS = "EXTRACT(EPOCH FROM (received_at - sent_at)) * 1000"


def _with_tip(row: Record) -> MessageWithTip:
    legs: list[dict[str, Any]] = json.loads(row["tip"])
    stored: list[dict[str, Any] | None] = (
        json.loads(row["offers"]) if row["offers"] else []
    )
    parsed = tuple(
        TipLeg(
            event=leg["event"],
            market=leg["market"],
            selection=leg["selection"],
            odds=leg["odds"],
        )
        for leg in legs
    )

    return MessageWithTip(
        message=_to_message(row),
        legs=parsed,
        offers=tuple(
            None
            if offer is None
            else LegOffer(
                leg=leg,
                event_id=offer["event_id"],
                event_name=offer["event_name"],
                offer_id=offer["offer_id"],
                odds=offer["odds"],
            )
            for leg, offer in zip(parsed, stored, strict=True)
        )
        if stored
        else (),
    )


def _to_message(row: Record) -> IncomingMessage:
    return IncomingMessage(
        external_id=row["external_id"],
        channel=row["channel"],
        sent_at=row["sent_at"],
        received_at=row["received_at"],
        text=row["text"],
        media_kind=row["media_kind"],
        media_path=row["media_path"],
    )


class MessageStore:
    """Append-only archive, owning its own connection pool."""

    def __init__(self, pool: Pool) -> None:
        """Wrap an open pool; use :meth:`connect` rather than calling this."""
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> MessageStore:
        """Open the pool and bring the schema up to date."""
        pool = await create_pool(
            dsn, min_size=1, max_size=5, max_inactive_connection_lifetime=300
        )
        await apply_migrations(pool)

        return cls(pool)

    async def add(self, message: IncomingMessage) -> bool:
        """Store a message; return False if we had already archived it."""
        row = await self._pool.fetchrow(
            f"""
            INSERT INTO messages ({_MESSAGE_COLUMNS})
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT DO NOTHING
            RETURNING external_id
            """,
            message.external_id,
            message.channel,
            message.sent_at,
            message.received_at,
            message.text,
            message.media_kind,
            message.media_path,
        )

        return row is not None

    async def set_tip(self, tip: Tip, offers: Sequence[LegOffer | None] = ()) -> None:
        """Record the legs read off a screenshot, and what each one resolved to."""
        await self._pool.execute(
            """
            UPDATE messages SET tip = $2, offers = $3
            WHERE external_id = $1
            """,
            tip.message.external_id,
            json.dumps([asdict(leg) for leg in tip.legs]),
            json.dumps(
                [
                    None
                    if offer is None
                    else {
                        "event_id": offer.event_id,
                        "event_name": offer.event_name,
                        "offer_id": offer.offer_id,
                        "odds": offer.odds,
                    }
                    for offer in offers
                ]
            )
            if offers
            else None,
        )

    async def totals(self) -> dict[str, int]:
        """Return archive counts, including how many tips the feed could place."""
        row = await self._pool.fetchrow(
            """
            SELECT count(*) AS messages,
                   count(tip) AS tips,
                   count(offers) AS tips_looked_up,
                   count(*) FILTER (
                       WHERE offers IS NOT NULL
                         AND NOT jsonb_path_exists(offers, '$[*] ? (@ == null)')
                   ) AS tips_fully_resolved
            FROM messages
            """
        )
        assert row is not None

        return dict(row.items())

    async def recent_tips(self, limit: int = 50) -> list[MessageWithTip]:
        """Return the most recent messages that parsed into a tip, newest first."""
        rows = await self._pool.fetch(
            f"""
            SELECT {_MESSAGE_COLUMNS}, tip, offers FROM messages
            WHERE tip IS NOT NULL
            ORDER BY received_at DESC LIMIT $1
            """,
            limit,
        )

        return [_with_tip(row) for row in rows]

    async def latency_percentiles(self) -> dict[str, int]:
        """Return p50/p90/p99 transport latency in ms."""
        row = await self._pool.fetchrow(
            f"""
            SELECT percentile_disc(0.50) WITHIN GROUP (ORDER BY ms) AS p50,
                   percentile_disc(0.90) WITHIN GROUP (ORDER BY ms) AS p90,
                   percentile_disc(0.99) WITHIN GROUP (ORDER BY ms) AS p99
            FROM (SELECT {_TRANSPORT_LATENCY_MS} AS ms FROM messages) AS latencies
            """
        )

        if row is None or row["p50"] is None:
            return {}

        return {name: int(value) for name, value in row.items()}

    async def close(self) -> None:
        """Close the pool."""
        await self._pool.close()
