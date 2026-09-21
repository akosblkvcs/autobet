"""Each person's account at a book, with the password sealed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from asyncpg import Pool

from autobet import crypto


@dataclass(frozen=True, slots=True)
class Account:
    """One person's account at one book, as everything but placement sees it."""

    id: int
    user_id: int
    username: str
    status: str
    last_login_at: datetime | None
    last_error: str


class Accounts:
    """The accounts a tip is staked through, and the credentials they hold."""

    def __init__(self, pool: Pool, key: str) -> None:
        """Share the store's pool, and hold the key that seals a password."""
        self._pool = pool
        self._key = key

    async def put(self, user_id: int, slug: str, username: str, password: str) -> None:
        """Store or replace someone's credentials for one book."""
        await self._pool.execute(
            """
            INSERT INTO bookmaker_accounts (user_id, bookmaker_id, username, secret)
            VALUES ($1, (SELECT id FROM bookmakers WHERE slug = $2), $3, $4)
            ON CONFLICT (user_id, bookmaker_id)
            DO UPDATE SET username = excluded.username,
                          secret = excluded.secret,
                          status = 'active',
                          last_error = ''
            """,
            user_id,
            slug,
            username,
            crypto.seal(password, self._key),
        )

    async def forget(self, user_id: int, slug: str) -> None:
        """Remove someone's account at one book, credentials and all."""
        await self._pool.execute(
            """
            DELETE FROM bookmaker_accounts
            WHERE user_id = $1
              AND bookmaker_id = (SELECT id FROM bookmakers WHERE slug = $2)
            """,
            user_id,
            slug,
        )

    async def active(self, slug: str) -> list[Account]:
        """The accounts a tip can be staked through, oldest first."""
        return [one for one in await self.all(slug) if one.status == "active"]

    async def all(self, slug: str) -> list[Account]:
        """Every account at one book, whatever its status, oldest first."""
        rows = await self._pool.fetch(
            """
            SELECT a.id, a.user_id, a.username, a.status, a.last_login_at,
                   a.last_error
            FROM bookmaker_accounts a
            JOIN bookmakers b ON b.id = a.bookmaker_id
            WHERE b.slug = $1
            ORDER BY a.created_at
            """,
            slug,
        )

        return [
            Account(
                id=row["id"],
                user_id=row["user_id"],
                username=row["username"],
                status=row["status"],
                last_login_at=row["last_login_at"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    async def credentials(self, account_id: int) -> tuple[str, str]:
        """The username and password to mint a session with."""
        row = await self._pool.fetchrow(
            "SELECT username, secret FROM bookmaker_accounts WHERE id = $1",
            account_id,
        )

        if row is None:
            raise ValueError(f"no bookmaker account {account_id}")

        return row["username"], crypto.unseal(row["secret"], self._key)
