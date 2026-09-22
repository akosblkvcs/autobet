"""Tips, what their legs resolved to, and what each user did about them."""

from asyncpg import Pool, Record
from asyncpg.pool import PoolConnectionProxy

from autobet.models import (
    BetResult,
    BetState,
    LegResolution,
    MessageWithTip,
    Placement,
    Refusal,
    RefusalCode,
    Tip,
    Verdict,
)
from autobet.storage.rows import to_leg, to_message, to_resolution


def _to_verdict(row: Record) -> Verdict:
    """One account's bet row as the tip list reads it."""
    return Verdict(
        who=row["who"],
        refusal=(
            None
            if row["refusal_code"] is None
            else Refusal(RefusalCode(row["refusal_code"]), row["refusal_detail"])
        ),
        error=row["refusal_detail"] if row["state"] == BetState.ERROR else "",
        reference=row["reference"],
        state=BetState(row["state"]),
    )


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


class Bets:
    """One write path for a verdict, and the read model the pages render."""

    def __init__(self, pool: Pool) -> None:
        """Share the store's pool."""
        self._pool = pool

    async def record(self, tip: Tip, placement: Placement) -> None:
        """Write the tip, its legs, what they resolved to and every bet, at once."""
        resolutions = placement.resolutions or (None,) * len(tip.legs)

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
            legs: list[tuple[int, int | None]] = []

            for position, (leg, resolution) in enumerate(
                zip(tip.legs, resolutions, strict=True)
            ):
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
                legs.append(
                    (leg_id, await self._write_selection(conn, leg_id, resolution))
                )

            for result in placement.results:
                bet_id = await self._write_bet(conn, tip_id, tip, result)
                priced = result.resolutions or (None,) * len(tip.legs)

                await self._write_bet_legs(conn, bet_id, legs, priced)

    async def _write_bet_legs(
        self,
        conn: PoolConnectionProxy[Record],
        bet_id: int,
        legs: list[tuple[int, int | None]],
        priced: tuple[LegResolution | None, ...],
    ) -> None:
        """One account's per-leg snapshot, priced as that account saw it."""
        for (leg_id, selection_id), resolution in zip(legs, priced, strict=True):
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
                resolution.offer.odds if resolution and resolution.offer else None,
            )

    async def _write_bet(
        self,
        conn: PoolConnectionProxy[Record],
        tip_id: int,
        tip: Tip,
        result: BetResult,
    ) -> int:
        """Write the verdict on the tip and return its bet id."""
        owned = "(tip_id, user_id)" if result.user_id is not None else "(tip_id)"
        unowned = "" if result.user_id is not None else " WHERE user_id IS NULL"
        bet_id: int = await conn.fetchval(
            f"""
            INSERT INTO bets (
                tip_id, user_id, state, refusal_code, refusal_detail,
                stake, odds, reference, placed_at, settlement
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT {owned}{unowned} DO UPDATE SET
                state = EXCLUDED.state,
                refusal_code = EXCLUDED.refusal_code,
                refusal_detail = EXCLUDED.refusal_detail,
                stake = EXCLUDED.stake,
                odds = EXCLUDED.odds,
                reference = EXCLUDED.reference,
                placed_at = EXCLUDED.placed_at,
                settlement = EXCLUDED.settlement
            RETURNING id
            """,
            tip_id,
            result.user_id,
            result.state,
            None if result.refusal is None else result.refusal.code,
            result.refusal.detail if result.refusal else result.error,
            result.stake,
            tip.odds,
            result.reference,
            result.placed_at,
            "pending" if result.state is BetState.PLACED else None,
        )

        return bet_id

    async def _write_selection(
        self,
        conn: PoolConnectionProxy[Record],
        leg_id: int,
        resolution: LegResolution | None,
    ) -> int | None:
        """Record what the leg resolved to, or the stage it stopped at."""
        if resolution is None:
            await conn.execute("DELETE FROM selections WHERE tip_leg_id = $1", leg_id)

            return None

        offer = resolution.offer
        selection_id: int | None = await conn.fetchval(
            _INSERT_SELECTION,
            leg_id,
            resolution.status,
            offer.event_id if offer else None,
            offer.event_name if offer else None,
            offer.market_id if offer else None,
            offer.outcome_id if offer else None,
            offer.betting_type_id if offer else None,
            offer.offer_id if offer else None,
            offer.odds if offer else None,
            offer.starts_at if offer else None,
        )

        return selection_id

    async def recent(self, limit: int = 50) -> list[MessageWithTip]:
        """The most recent tips that reached a verdict, newest first."""
        rows = await self._pool.fetch(
            """
            SELECT m.external_id, c.title AS channel, m.sent_at, m.received_at,
                   m.text, m.media_path, t.id AS tip_id
            FROM tips t
            JOIN messages m ON m.id = t.message_id
            JOIN channels c ON c.id = m.channel_id
            WHERE EXISTS (SELECT 1 FROM bets b WHERE b.tip_id = t.id)
            ORDER BY m.received_at DESC
            LIMIT $1
            """,
            limit,
        )
        if not rows:
            return []

        leg_rows = await self._pool.fetch(
            """
            SELECT l.*, s.status, s.event_id, s.event_name, s.market_id, s.outcome_id,
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

        bet_rows = await self._pool.fetch(
            """
            SELECT b.tip_id, b.state, b.refusal_code, b.refusal_detail,
                   coalesce(b.reference, '') AS reference,
                   coalesce(u.email, '') AS who
            FROM bets b
            LEFT JOIN users u ON u.id = b.user_id
            WHERE b.tip_id = ANY($1::bigint[])
            ORDER BY b.tip_id, b.id
            """,
            [row["tip_id"] for row in rows],
        )
        verdicts: dict[int, list[Verdict]] = {}
        for bet_row in bet_rows:
            verdicts.setdefault(bet_row["tip_id"], []).append(_to_verdict(bet_row))

        found: list[MessageWithTip] = []
        for row in rows:
            legs = tuple(to_leg(one) for one in by_tip.get(row["tip_id"], []))
            resolutions = tuple(
                to_resolution(one, leg)
                for one, leg in zip(by_tip.get(row["tip_id"], []), legs, strict=True)
            )
            found.append(
                MessageWithTip(
                    message=to_message(row),
                    legs=legs,
                    resolutions=resolutions,
                    verdicts=tuple(verdicts.get(row["tip_id"], [])),
                )
            )

        return found
