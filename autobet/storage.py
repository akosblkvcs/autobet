"""Postgres archive of every message we observed, and what became of it."""

from __future__ import annotations

from asyncpg import Pool, Record, create_pool

from autobet.migrate import apply_migrations
from autobet.models import (
    BetResult,
    IncomingMessage,
    LegOffer,
    MessageWithTip,
    Tip,
    TipLeg,
)

_MESSAGE_COLUMNS = (
    "external_id, channel, sent_at, received_at, text, media_kind, media_path"
)
# Derived in SQL rather than stored, so there is one source of truth.
_TRANSPORT_LATENCY_MS = "EXTRACT(EPOCH FROM (received_at - sent_at)) * 1000"


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


def _to_leg(row: Record) -> TipLeg:
    return TipLeg(
        event=row["event"],
        market=row["market"],
        selection=row["selection"],
        odds=float(row["odds"]),
    )


def _to_offer(row: Record, leg: TipLeg) -> LegOffer | None:
    """Rebuild what the leg resolved to, or None if it never did."""
    if not row["offer_id"]:
        return None

    return LegOffer(
        leg=leg,
        event_id=row["event_id"],
        event_name=row["event_name"],
        market_id=row["market_id"],
        outcome_id=row["outcome_id"],
        betting_type_id=row["betting_type_id"],
        offer_id=row["offer_id"],
        odds=float(row["live_odds"]),
        starts_at=row["starts_at"],
    )


class MessageStore:
    """The archive, owning its own connection pool."""

    def __init__(self, pool: Pool) -> None:
        """Wrap an open pool; use :meth:`connect` rather than calling this."""
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> MessageStore:
        """Open the pool and bring the schema up to date."""
        # Coolify's proxy drops idle connections, so retire them before it does.
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
            RETURNING id
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

    async def record(self, tip: Tip, result: BetResult | None = None) -> None:
        """Write the tip, its legs and the bet, as one transaction."""
        offers = result.offers if result else ()

        async with self._pool.acquire() as conn, conn.transaction():
            tip_id: int = await conn.fetchval(
                """
                INSERT INTO tips (message_id, stake)
                VALUES ((SELECT id FROM messages WHERE external_id = $1), $2)
                ON CONFLICT (message_id) DO UPDATE SET stake = EXCLUDED.stake
                RETURNING id
                """,
                tip.message.external_id,
                tip.stake,
            )
            await conn.execute("DELETE FROM legs WHERE tip_id = $1", tip_id)

            for position, leg in enumerate(tip.legs):
                offer = offers[position] if position < len(offers) else None
                await conn.execute(
                    """
                    INSERT INTO legs (
                        tip_id, position, event, market, selection, odds,
                        event_id, event_name, market_id, outcome_id,
                        betting_type_id, offer_id, live_odds, starts_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    """,
                    tip_id,
                    position,
                    leg.event,
                    leg.market,
                    leg.selection,
                    leg.odds,
                    offer.event_id if offer else None,
                    offer.event_name if offer else None,
                    offer.market_id if offer else None,
                    offer.outcome_id if offer else None,
                    offer.betting_type_id if offer else None,
                    offer.offer_id if offer else None,
                    offer.odds if offer else None,
                    offer.starts_at if offer else None,
                )

            if result is not None:
                await conn.execute(
                    """
                    INSERT INTO bets (tip_id, placed_at, reference, refusal, stake, odds)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (tip_id) DO UPDATE SET
                        placed_at = EXCLUDED.placed_at,
                        reference = EXCLUDED.reference,
                        refusal   = EXCLUDED.refusal
                    """,
                    tip_id,
                    result.placed_at,
                    result.reference,
                    result.refusal,
                    tip.stake,
                    tip.odds,
                )

    async def totals(self) -> dict[str, int]:
        """Archive counts, including how the book answered."""
        row = await self._pool.fetchrow(
            """
            SELECT (SELECT count(*) FROM messages) AS messages,
                   (SELECT count(*) FROM tips)     AS tips,
                   (SELECT count(*) FROM bets WHERE refusal = '') AS bets_placed,
                   (SELECT count(*) FROM bets WHERE refusal <> '') AS bets_refused,
                   (SELECT count(*) FROM legs WHERE offer_id IS NULL) AS legs_unresolved
            """
        )
        assert row is not None

        return dict(row.items())

    async def recent_tips(self, limit: int = 50) -> list[MessageWithTip]:
        """The most recent tips with their legs and outcome, newest first."""
        rows = await self._pool.fetch(
            f"""
            SELECT m.{_MESSAGE_COLUMNS.replace(", ", ", m.")},
                   t.id AS tip_id,
                   coalesce(b.refusal, '')   AS refusal,
                   coalesce(b.reference, '') AS reference
            FROM tips t
            JOIN messages m ON m.id = t.message_id
            LEFT JOIN bets b ON b.tip_id = t.id
            ORDER BY m.received_at DESC
            LIMIT $1
            """,
            limit,
        )
        if not rows:
            return []

        leg_rows = await self._pool.fetch(
            """
            SELECT * FROM legs WHERE tip_id = ANY($1::bigint[])
            ORDER BY tip_id, position
            """,
            [row["tip_id"] for row in rows],
        )
        by_tip: dict[int, list[Record]] = {}
        for leg_row in leg_rows:
            by_tip.setdefault(leg_row["tip_id"], []).append(leg_row)

        found: list[MessageWithTip] = []
        for row in rows:
            legs = tuple(_to_leg(one) for one in by_tip.get(row["tip_id"], []))
            offers = tuple(
                _to_offer(one, leg)
                for one, leg in zip(by_tip.get(row["tip_id"], []), legs, strict=True)
            )
            found.append(
                MessageWithTip(
                    message=_to_message(row),
                    legs=legs,
                    offers=offers,
                    refusal=row["refusal"],
                    reference=row["reference"],
                )
            )

        return found

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
