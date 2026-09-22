"""Read-only summaries: what the archive holds and how fast it arrived."""

from asyncpg import Pool

# Derived in SQL rather than stored, so there is one source of truth.
_TRANSPORT_LATENCY_MS = "EXTRACT(EPOCH FROM (received_at - sent_at)) * 1000"


class Reports:
    """Counts and percentiles, none of which any write path depends on."""

    def __init__(self, pool: Pool) -> None:
        """Share the store's pool."""
        self._pool = pool

    async def totals(self) -> dict[str, int]:
        """Archive counts, including how the book answered."""
        row = await self._pool.fetchrow(
            """
            SELECT (SELECT count(*) FROM messages) AS messages,
                   (SELECT count(*) FROM tips)     AS tips,
                   (SELECT count(*) FROM bets WHERE state = 'placed')  AS bets_placed,
                   (SELECT count(*) FROM bets WHERE state = 'paper')   AS bets_paper,
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
