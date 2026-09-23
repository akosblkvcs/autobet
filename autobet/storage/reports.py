"""Read-only summaries: what the archive holds and how fast it moved."""

from typing import Any

from asyncpg import Pool

_PERCENTILES = ("p50", "p90", "p95", "p99")
_LATENCY = """
    WITH samples AS (
        SELECT 'transport' AS stage,
               EXTRACT(EPOCH FROM (m.received_at - m.sent_at)) * 1000 AS ms
        FROM messages m
        UNION ALL
        SELECT 'parse', EXTRACT(EPOCH FROM (t.parsed_at - m.received_at)) * 1000
        FROM tips t
          JOIN messages m ON m.id = t.message_id
        UNION ALL
        SELECT 'resolve', EXTRACT(EPOCH FROM (s.resolved_at - t.parsed_at)) * 1000
        FROM selections s
          JOIN tip_legs l ON l.id = s.tip_leg_id
          JOIN tips t ON t.id = l.tip_id
        UNION ALL
        SELECT 'end to end', EXTRACT(EPOCH FROM (b.placed_at - m.received_at)) * 1000
        FROM bets b
          JOIN tips t ON t.id = b.tip_id
          JOIN messages m ON m.id = t.message_id
    ),
    stages (stage, sort) AS (
        VALUES ('transport', 1), ('parse', 2), ('resolve', 3), ('end to end', 4)
    )
    SELECT g.stage,
           count(s.ms)                                        AS samples,
           percentile_disc(0.50) WITHIN GROUP (ORDER BY s.ms)  AS p50,
           percentile_disc(0.90) WITHIN GROUP (ORDER BY s.ms)  AS p90,
           percentile_disc(0.95) WITHIN GROUP (ORDER BY s.ms)  AS p95,
           percentile_disc(0.99) WITHIN GROUP (ORDER BY s.ms)  AS p99,
           round(avg(s.ms))                                    AS mean,
           max(s.ms)                                           AS slowest
    FROM stages g
      LEFT JOIN samples s ON s.stage = g.stage
    GROUP BY g.stage, g.sort
    ORDER BY g.sort
"""


class Reports:
    """Counts and percentiles, none of which any write path depends on."""

    def __init__(self, pool: Pool) -> None:
        """Share the store's pool."""
        self._pool = pool

    async def totals(self, user_id: int) -> dict[str, int]:
        """The tips everyone saw, and what became of this user's bets on them."""
        row = await self._pool.fetchrow(
            """
            SELECT (SELECT count(*) FROM tips) AS tips,
                   (SELECT count(*) FROM bets
                      WHERE user_id = $1 AND state = 'placed')  AS placed,
                   (SELECT count(*) FROM bets
                      WHERE user_id = $1 AND state = 'paper')   AS paper,
                   (SELECT count(*) FROM bets
                      WHERE user_id = $1 AND state = 'refused') AS refused,
                   (SELECT count(*) FROM bets
                      WHERE user_id = $1 AND state = 'error')   AS failed
            """,
            user_id,
        )
        assert row is not None

        return dict(row.items())

    async def latency(self) -> list[dict[str, Any]]:
        """Milliseconds per pipeline stage, whoever the tip was eventually for."""
        return [
            {
                "stage": row["stage"],
                "samples": row["samples"],
                **{
                    name: None if row[name] is None else int(row[name])
                    for name in (*_PERCENTILES, "mean", "slowest")
                },
            }
            for row in await self._pool.fetch(_LATENCY)
        ]
