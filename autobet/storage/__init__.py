"""Postgres storage: one pool, and a repository per aggregate."""

from __future__ import annotations

from asyncpg import Pool, create_pool

from autobet.storage.accounts import Accounts
from autobet.storage.archive import Archive
from autobet.storage.bets import Bets
from autobet.storage.books import Books
from autobet.storage.config import Config
from autobet.storage.events import Events
from autobet.storage.migrate import apply_migrations
from autobet.storage.reports import Reports
from autobet.storage.users import Users


class Store:
    """The pool every repository shares, and the repositories themselves."""

    def __init__(self, pool: Pool, key: str) -> None:
        """Wrap an open pool; use :meth:`connect` rather than calling this."""
        self._pool = pool
        self.accounts = Accounts(pool, key)
        self.archive = Archive(pool)
        self.bets = Bets(pool)
        self.books = Books(pool)
        self.config = Config(pool)
        self.events = Events(pool)
        self.reports = Reports(pool)
        self.users = Users(pool)

    @classmethod
    async def connect(cls, dsn: str, key: str) -> Store:
        """Open the pool and bring the schema up to date."""
        pool = await create_pool(
            dsn, min_size=1, max_size=5, max_inactive_connection_lifetime=300
        )
        await apply_migrations(pool)

        return cls(pool, key)

    async def close(self) -> None:
        """Close the pool, and with it every repository."""
        await self._pool.close()
