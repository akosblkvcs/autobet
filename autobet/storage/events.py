"""The event index the scheduled walk writes and every tip reads."""

from collections.abc import Mapping, Sequence
from datetime import datetime

from asyncpg import Pool

from autobet.matching import IndexedEvent
from autobet.storage.rows import event_row, to_event

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


class Events:
    """The board as rows: replaced by a full walk, patched by a re-walk."""

    def __init__(self, pool: Pool) -> None:
        """Share the store's pool."""
        self._pool = pool

    async def replace(
        self, events: Sequence[IndexedEvent], upcoming: Mapping[str, int]
    ) -> None:
        """Write a whole walk of the board, replacing the one before it."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("TRUNCATE events, tournaments")
            await conn.executemany(_INSERT_TOURNAMENT, list(upcoming.items()))
            await conn.executemany(_INSERT_EVENT, [event_row(event) for event in events])

    async def update(
        self, events: Sequence[IndexedEvent], upcoming: Mapping[str, int]
    ) -> None:
        """Write back the tournaments one re-walk re-read, leaving the rest alone."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.executemany(_INSERT_TOURNAMENT, list(upcoming.items()))
            await conn.executemany(_INSERT_EVENT, [event_row(event) for event in events])

    async def all(self) -> list[IndexedEvent]:
        """The whole index, as the matcher wants it."""
        rows = await self._pool.fetch("SELECT * FROM events")

        return [to_event(row) for row in rows]

    async def upcoming(self) -> dict[str, int]:
        """What each tournament declared when it was walked."""
        rows = await self._pool.fetch("SELECT id, upcoming FROM tournaments")

        return {row["id"]: row["upcoming"] for row in rows}

    async def freshness(self) -> tuple[datetime | None, int]:
        """How fresh the whole index is -- its oldest row -- and how big."""
        row = await self._pool.fetchrow(
            "SELECT min(indexed_at) AS at, count(*) AS events FROM events"
        )
        assert row is not None

        return row["at"], row["events"]
