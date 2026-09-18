"""Postgres storage: every message we observed, and everything it became."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from asyncpg import Pool, Record, create_pool
from asyncpg.pool import PoolConnectionProxy

from autobet.matching import IndexedEvent
from autobet.migrate import apply_migrations
from autobet.models import (
    BetResult,
    BetState,
    IncomingMessage,
    LegOffer,
    MessageWithTip,
    Refusal,
    RefusalCode,
    SelectionStatus,
    Tip,
    TipLeg,
)

# The operator owns every bet until logins arrive; seeded by the schema.
OPERATOR_EMAIL = "operator@autobet.local"

_INSERT_EVENT = """
    INSERT INTO events (
        id, tournament_id, name, sport, home_id, away_id, home, away, starts_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (id) DO UPDATE SET
        tournament_id = EXCLUDED.tournament_id,
        name = EXCLUDED.name, sport = EXCLUDED.sport,
        home_id = EXCLUDED.home_id, away_id = EXCLUDED.away_id,
        home = EXCLUDED.home, away = EXCLUDED.away,
        starts_at = EXCLUDED.starts_at, indexed_at = now()
"""
_INSERT_TOURNAMENT = """
    INSERT INTO tournaments (id, upcoming) VALUES ($1, $2)
    ON CONFLICT (id) DO UPDATE SET upcoming = EXCLUDED.upcoming, indexed_at = now()
"""
_INSERT_SELECTION = """
    INSERT INTO selections (
        tip_leg_id, status, event_id, event_name, market_id, outcome_id,
        betting_type_id, offer_id, odds, starts_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (tip_leg_id) DO UPDATE SET
        status = EXCLUDED.status,
        event_id = EXCLUDED.event_id, event_name = EXCLUDED.event_name,
        market_id = EXCLUDED.market_id, outcome_id = EXCLUDED.outcome_id,
        betting_type_id = EXCLUDED.betting_type_id, offer_id = EXCLUDED.offer_id,
        odds = EXCLUDED.odds, starts_at = EXCLUDED.starts_at, resolved_at = now()
    RETURNING id
"""
# Derived in SQL rather than stored, so there is one source of truth.
_TRANSPORT_LATENCY_MS = "EXTRACT(EPOCH FROM (received_at - sent_at)) * 1000"


def _to_message(row: Record) -> IncomingMessage:
    return IncomingMessage(
        external_id=row["external_id"],
        channel=row["channel"],
        sent_at=row["sent_at"],
        received_at=row["received_at"],
        text=row["text"],
        media_path=row["media_path"],
    )


def _to_leg(row: Record) -> TipLeg:
    return TipLeg(
        event=row["event"],
        market=row["market"],
        selection=row["selection"],
        odds=None if row["odds"] is None else float(row["odds"]),
        sport=row["sport"],
    )


def _to_event(row: Record) -> IndexedEvent:
    return IndexedEvent(
        id=row["id"],
        tournament_id=row["tournament_id"],
        name=row["name"],
        sport=row["sport"],
        home_id=row["home_id"],
        away_id=row["away_id"],
        home=tuple(row["home"]),
        away=tuple(row["away"]),
        starts_at=row["starts_at"],
    )


def _row(event: IndexedEvent) -> tuple[object, ...]:
    """One event as the insert's parameters."""
    return (
        event.id,
        event.tournament_id,
        event.name,
        event.sport,
        event.home_id,
        event.away_id,
        list(event.home),
        list(event.away),
        event.starts_at,
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


class Store:
    """Every table the app writes, owning its own connection pool."""

    def __init__(self, pool: Pool) -> None:
        """Wrap an open pool; use :meth:`connect` rather than calling this."""
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> Store:
        """Open the pool and bring the schema up to date."""
        pool = await create_pool(
            dsn, min_size=1, max_size=5, max_inactive_connection_lifetime=300
        )
        await apply_migrations(pool)

        return cls(pool)

    async def register_channel(self, chat_id: int, title: str) -> None:
        """Name a watched chat once, so no row ever shows a bare id."""
        await self._pool.execute(
            """
            INSERT INTO channels (chat_id, title) VALUES ($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title
            """,
            chat_id,
            title,
        )

    async def add(self, message: IncomingMessage) -> bool:
        """Store a message; return False if we had already archived it."""
        async with self._pool.acquire() as conn, conn.transaction():
            channel_id: int = await conn.fetchval(
                """
                INSERT INTO channels (chat_id, title) VALUES ($1, $2)
                ON CONFLICT (chat_id) DO UPDATE SET chat_id = EXCLUDED.chat_id
                RETURNING id
                """,
                message.chat_id,
                message.channel,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO messages (
                    channel_id, external_id, sent_at, received_at, text, media_path
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                channel_id,
                message.external_id,
                message.sent_at,
                message.received_at,
                message.text,
                message.media_path,
            )

        return row is not None

    async def record(self, tip: Tip, result: BetResult) -> None:
        """Write the tip, its legs, what they resolved to and the bet, at once."""
        offers = result.offers or (None,) * len(tip.legs)

        async with self._pool.acquire() as conn, conn.transaction():
            tip_id: int = await conn.fetchval(
                """
                INSERT INTO tips (message_id, combined_odds)
                VALUES ((SELECT id FROM messages WHERE external_id = $1), $2)
                ON CONFLICT (message_id) DO UPDATE SET
                    combined_odds = EXCLUDED.combined_odds
                RETURNING id
                """,
                tip.message.external_id,
                tip.odds,
            )
            bet_id = await self._write_bet(conn, tip_id, tip, result)

            for position, (leg, offer) in enumerate(zip(tip.legs, offers, strict=True)):
                leg_id: int = await conn.fetchval(
                    """
                    INSERT INTO tip_legs (
                        tip_id, position, sport, event, market, selection, odds
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (tip_id, position) DO UPDATE SET
                        sport = EXCLUDED.sport, event = EXCLUDED.event,
                        market = EXCLUDED.market, selection = EXCLUDED.selection,
                        odds = EXCLUDED.odds
                    RETURNING id
                    """,
                    tip_id,
                    position,
                    leg.sport,
                    leg.event,
                    leg.market,
                    leg.selection,
                    leg.odds,
                )
                selection_id = await self._write_selection(conn, leg_id, offer)

                await conn.execute(
                    """
                    INSERT INTO bet_legs (bet_id, tip_leg_id, selection_id, odds)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (bet_id, tip_leg_id) DO UPDATE SET
                        selection_id = EXCLUDED.selection_id, odds = EXCLUDED.odds
                    """,
                    bet_id,
                    leg_id,
                    selection_id,
                    None if offer is None else offer.odds,
                )

    async def _write_bet(
        self,
        conn: PoolConnectionProxy[Record],
        tip_id: int,
        tip: Tip,
        result: BetResult,
    ) -> int:
        """Write this user's verdict on the tip and return its id."""
        bet_id: int = await conn.fetchval(
            """
            INSERT INTO bets (
                tip_id, user_id, state, refusal_code, refusal_detail,
                stake, odds, reference, placed_at, settlement
            )
            VALUES (
                $1, (SELECT id FROM users WHERE email = $2), $3, $4, $5,
                $6, $7, $8, $9, $10
            )
            ON CONFLICT (tip_id, user_id) DO UPDATE SET
                state = EXCLUDED.state,
                refusal_code = EXCLUDED.refusal_code,
                refusal_detail = EXCLUDED.refusal_detail,
                odds = EXCLUDED.odds,
                reference = EXCLUDED.reference,
                placed_at = EXCLUDED.placed_at,
                settlement = EXCLUDED.settlement
            RETURNING id
            """,
            tip_id,
            OPERATOR_EMAIL,
            result.state,
            None if result.refusal is None else result.refusal.code,
            result.refusal.detail if result.refusal else result.error,
            tip.stake,
            tip.odds,
            result.reference,
            result.placed_at,
            "pending" if result.state is BetState.PLACED else None,
        )

        return bet_id

    async def _write_selection(
        self, conn: PoolConnectionProxy[Record], leg_id: int, offer: LegOffer | None
    ) -> int | None:
        """Record what the leg resolved to, or nothing while it resolved to nothing."""
        if offer is None:
            return None

        selection_id: int | None = await conn.fetchval(
            _INSERT_SELECTION,
            leg_id,
            SelectionStatus.RESOLVED,
            offer.event_id,
            offer.event_name,
            offer.market_id,
            offer.outcome_id,
            offer.betting_type_id,
            offer.offer_id,
            offer.odds,
            offer.starts_at,
        )

        return selection_id

    async def replace_index(
        self, events: Sequence[IndexedEvent], upcoming: Mapping[str, int]
    ) -> None:
        """Write a whole walk of the board, replacing the one before it."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("TRUNCATE events, tournaments")
            await conn.executemany(_INSERT_TOURNAMENT, list(upcoming.items()))
            await conn.executemany(_INSERT_EVENT, [_row(event) for event in events])

    async def update_index(
        self, events: Sequence[IndexedEvent], upcoming: Mapping[str, int]
    ) -> None:
        """Write back the tournaments one re-walk re-read, leaving the rest alone."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.executemany(_INSERT_TOURNAMENT, list(upcoming.items()))
            await conn.executemany(_INSERT_EVENT, [_row(event) for event in events])

    async def events(self) -> list[IndexedEvent]:
        """The whole index, as the matcher wants it."""
        rows = await self._pool.fetch("SELECT * FROM events")

        return [_to_event(row) for row in rows]

    async def upcoming(self) -> dict[str, int]:
        """What each tournament declared when it was walked."""
        rows = await self._pool.fetch("SELECT id, upcoming FROM tournaments")

        return {row["id"]: row["upcoming"] for row in rows}

    async def indexed(self) -> tuple[datetime | None, int]:
        """How fresh the whole index is -- its oldest row -- and how big.

        The oldest row, not the newest: a re-walk touches a few tournaments,
        and that must not make the rest of the board look freshly read.
        """
        row = await self._pool.fetchrow(
            "SELECT min(indexed_at) AS at, count(*) AS events FROM events"
        )
        assert row is not None

        return row["at"], row["events"]

    async def totals(self) -> dict[str, int]:
        """Archive counts, including how the book answered."""
        row = await self._pool.fetchrow(
            """
            SELECT (SELECT count(*) FROM messages) AS messages,
                   (SELECT count(*) FROM tips)     AS tips,
                   (SELECT count(*) FROM bets WHERE state = 'placed')  AS bets_placed,
                   (SELECT count(*) FROM bets WHERE state = 'refused') AS bets_refused,
                   (SELECT count(*) FROM bets WHERE state = 'error')   AS bets_failed,
                   (SELECT count(*) FROM tip_legs l
                      LEFT JOIN selections s ON s.tip_leg_id = l.id
                      WHERE s.id IS NULL OR s.status <> 'resolved')
                       AS legs_unresolved
            """
        )
        assert row is not None

        return dict(row.items())

    async def recent_tips(self, limit: int = 50) -> list[MessageWithTip]:
        """The most recent tips with their legs and outcome, newest first."""
        rows = await self._pool.fetch(
            """
            SELECT m.external_id, c.title AS channel, m.sent_at, m.received_at,
                   m.text, m.media_path,
                   t.id AS tip_id,
                   b.state, b.refusal_code, b.refusal_detail,
                   coalesce(b.reference, '') AS reference
            FROM tips t
            JOIN messages m ON m.id = t.message_id
            JOIN channels c ON c.id = m.channel_id
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
            SELECT l.*, s.event_id, s.event_name, s.market_id, s.outcome_id,
                   s.betting_type_id, s.offer_id, s.odds AS live_odds, s.starts_at
            FROM tip_legs l
            LEFT JOIN selections s ON s.tip_leg_id = l.id
            WHERE l.tip_id = ANY($1::bigint[])
            ORDER BY l.tip_id, l.position
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
                    state=None if row["state"] is None else BetState(row["state"]),
                    refusal=(
                        None
                        if row["refusal_code"] is None
                        else Refusal(
                            RefusalCode(row["refusal_code"]), row["refusal_detail"]
                        )
                    ),
                    error=(
                        row["refusal_detail"] if row["state"] == BetState.ERROR else ""
                    ),
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
