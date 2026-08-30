"""Postgres archive of every message we observed, from any source."""

from asyncpg import Pool, Record, create_pool

from autobet.migrate import apply_migrations
from autobet.models import IncomingMessage

_COLUMNS = (
    "source, external_id, channel, sent_at, received_at, text, media_kind, media_path"
)
# Latency is derived in SQL rather than stored, so there is one source of truth.
_LATENCY_MS = "EXTRACT(EPOCH FROM (received_at - sent_at)) * 1000"


def _to_message(row: Record) -> IncomingMessage:
    return IncomingMessage(
        source=row["source"],
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
            INSERT INTO messages ({_COLUMNS})
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT DO NOTHING
            RETURNING external_id
            """,
            message.source,
            message.external_id,
            message.channel,
            message.sent_at,
            message.received_at,
            message.text,
            message.media_kind,
            message.media_path,
        )

        return row is not None

    async def count(self) -> int:
        """Return the number of archived messages."""
        return int(await self._pool.fetchval("SELECT COUNT(*) FROM messages"))

    async def recent(self, limit: int = 50) -> list[IncomingMessage]:
        """Return the most recently received messages, newest first."""
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM messages ORDER BY received_at DESC LIMIT $1",
            limit,
        )

        return [_to_message(row) for row in rows]

    async def latency_percentiles(self) -> dict[str, int]:
        """Return p50/p90/p99 transport latency in ms."""
        row = await self._pool.fetchrow(
            f"""
            SELECT percentile_disc(0.50) WITHIN GROUP (ORDER BY {_LATENCY_MS}) AS p50,
                   percentile_disc(0.90) WITHIN GROUP (ORDER BY {_LATENCY_MS}) AS p90,
                   percentile_disc(0.99) WITHIN GROUP (ORDER BY {_LATENCY_MS}) AS p99
            FROM messages
            """
        )

        if row is None or row["p50"] is None:  # nothing archived yet
            return {}

        return {name: int(value) for name, value in row.items()}

    async def close(self) -> None:
        """Close the pool."""
        await self._pool.close()
