"""The archive: the chats watched and every message they carried."""

from asyncpg import Pool

from autobet.models import IncomingMessage


class Archive:
    """Channels and messages, the only tables ingestion writes."""

    def __init__(self, pool: Pool) -> None:
        """Share the store's pool."""
        self._pool = pool

    async def enabled_chats(self) -> list[int]:
        """Every chat ingestion listens to, in id order."""
        rows = await self._pool.fetch(
            "SELECT chat_id FROM channels WHERE enabled ORDER BY chat_id"
        )

        return [row["chat_id"] for row in rows]

    async def is_watched(self, chat_id: int) -> bool:
        """Whether this chat is enabled, asked per message so a change is live."""
        return bool(
            await self._pool.fetchval(
                "SELECT enabled FROM channels WHERE chat_id = $1", chat_id
            )
        )

    async def channels(self) -> list[tuple[int, str, bool]]:
        """Every chat ever watched, enabled or not."""
        rows = await self._pool.fetch(
            "SELECT chat_id, title, enabled FROM channels ORDER BY chat_id"
        )

        return [(row["chat_id"], row["title"], row["enabled"]) for row in rows]

    async def watch(self, chat_id: int) -> None:
        """Start listening to a chat, adding it if it is new."""
        await self._pool.execute(
            """
            INSERT INTO channels (chat_id, title) VALUES ($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET enabled = true
            """,
            chat_id,
            str(chat_id),
        )

    async def unwatch(self, chat_id: int) -> None:
        """Stop listening to a chat."""
        await self._pool.execute(
            "UPDATE channels SET enabled = false WHERE chat_id = $1", chat_id
        )

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
