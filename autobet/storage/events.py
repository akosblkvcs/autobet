"""The event index the scheduled walk writes and every tip reads."""

from collections.abc import Sequence
from datetime import datetime

from asyncpg import Pool

from autobet.matching import IndexedEvent, IndexedTournament, fold
from autobet.storage.rows import event_row, to_event

_INSERT_EVENT = """
    INSERT INTO events (
        id, tournament_id, name, sport, home_id, away_id, home, away, starts_at,
        markets
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (id) DO UPDATE SET
        tournament_id = EXCLUDED.tournament_id,
        name = EXCLUDED.name, sport = EXCLUDED.sport,
        home_id = EXCLUDED.home_id, away_id = EXCLUDED.away_id,
        home = EXCLUDED.home, away = EXCLUDED.away,
        starts_at = EXCLUDED.starts_at, markets = EXCLUDED.markets,
        indexed_at = now()
"""
_INSERT_TOURNAMENT = """
    INSERT INTO tournaments (id, sport_id, upcoming) VALUES ($1, $2, $3)
    ON CONFLICT (id) DO UPDATE SET sport_id = EXCLUDED.sport_id,
                                   upcoming = EXCLUDED.upcoming,
                                   indexed_at = now()
"""


def _rows(walked: Sequence[IndexedTournament]) -> list[tuple[str, str, int]]:
    """Each walked tournament as the insert's arguments."""
    return [(one.id, one.sport_id, one.upcoming) for one in walked]


class Events:
    """The board as rows: replaced by a full walk, patched by a re-walk."""

    def __init__(self, pool: Pool) -> None:
        """Share the store's pool."""
        self._pool = pool

    async def replace(
        self, events: Sequence[IndexedEvent], walked: Sequence[IndexedTournament]
    ) -> None:
        """Write a whole walk of the board, replacing the one before it."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("TRUNCATE events, tournaments")
            await conn.executemany(_INSERT_TOURNAMENT, _rows(walked))
            await conn.executemany(_INSERT_EVENT, [event_row(event) for event in events])

    async def update(
        self, events: Sequence[IndexedEvent], walked: Sequence[IndexedTournament]
    ) -> None:
        """Replace the tournaments a re-walk re-read, leaving the rest alone."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.executemany(_INSERT_TOURNAMENT, _rows(walked))
            await conn.execute(
                "DELETE FROM events WHERE tournament_id = ANY($1::text[])",
                [tournament.id for tournament in walked],
            )
            await conn.executemany(_INSERT_EVENT, [event_row(event) for event in events])

    async def all(self) -> list[IndexedEvent]:
        """The whole index, as the matcher wants it."""
        rows = await self._pool.fetch("SELECT * FROM events")

        return [to_event(row) for row in rows]

    async def search(self, query: str, limit: int = 60) -> list[IndexedEvent]:
        """Indexed fixtures whose name holds these words, soonest first."""
        wanted = fold(query).split()
        found = [
            event
            for event in await self.all()
            if all(word in fold(event.name) for word in wanted)
        ]

        return sorted(found, key=lambda one: (one.starts_at is None, one.starts_at))[
            :limit
        ]

    async def upcoming(self) -> dict[str, int]:
        """What each tournament declared when it was walked."""
        rows = await self._pool.fetch("SELECT id, upcoming FROM tournaments")

        return {row["id"]: row["upcoming"] for row in rows}

    async def sports_of(self, sport: str) -> list[str]:
        """The feed ids a sport's fixtures came from, as the walk recorded them."""
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT t.sport_id
            FROM tournaments t JOIN events e ON e.tournament_id = t.id
            WHERE e.sport = $1 AND t.sport_id <> ''
            """,
            sport,
        )

        return [row["sport_id"] for row in rows]

    async def freshness(self) -> tuple[datetime | None, int]:
        """How fresh the whole index is -- its oldest row -- and how big."""
        row = await self._pool.fetchrow(
            "SELECT min(indexed_at) AS at, count(*) AS events FROM events"
        )
        assert row is not None

        return row["at"], row["events"]
