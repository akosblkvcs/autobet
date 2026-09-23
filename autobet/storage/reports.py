"""Read-only summaries: what the archive holds and how fast it moved."""

from typing import Any

from asyncpg import Pool

_PERCENTILES = ("p50", "p90", "p95", "p99")
_LATENCY = """
    WITH stamps AS (
        SELECT m.sent_at, m.received_at, b.placed_at
        FROM bets b
          JOIN tips t ON t.id = b.tip_id
          JOIN messages m ON m.id = t.message_id
    ),
    samples AS (
        SELECT s.stage, s.ms
        FROM stamps
          CROSS JOIN LATERAL (VALUES
              ('transport',
               EXTRACT(EPOCH FROM (stamps.received_at - stamps.sent_at)) * 1000),
              ('to bet',
               EXTRACT(EPOCH FROM (stamps.placed_at - stamps.received_at)) * 1000),
              ('total',
               EXTRACT(EPOCH FROM (stamps.placed_at - stamps.sent_at)) * 1000)
          ) AS s(stage, ms)
    ),
    stages (stage, sort) AS (
        VALUES ('transport', 1), ('to bet', 2), ('total', 3)
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
