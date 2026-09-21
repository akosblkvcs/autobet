"""Postgres storage: one pool, and a repository per aggregate."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from asyncpg import Pool, create_pool

from autobet.storage.archive import Archive
from autobet.storage.audit import Audit
from autobet.storage.bets import Bets
from autobet.storage.books import Books
from autobet.storage.config import Config
from autobet.storage.events import Events
from autobet.storage.migrate import apply_migrations
from autobet.storage.reports import Reports
from autobet.storage.rows import Executes
from autobet.storage.users import Users


class Store:
    """The pool every repository shares, and the repositories themselves."""

    def __init__(self, pool: Pool) -> None:
        """Wrap an open pool; use :meth:`connect` rather than calling this."""
        self._pool = pool
        self.archive = Archive(pool)
        self.audit = Audit(pool)
        self.bets = Bets(pool)
        self.books = Books(pool)
        self.config = Config(pool)
        self.events = Events(pool)
        self.reports = Reports(pool)
        self.users = Users(pool)

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[Executes]:
        """One connection in one transaction, for callers that must land together."""
        async with self._pool.acquire() as conn, conn.transaction():
            yield conn

    @classmethod
    async def connect(cls, dsn: str) -> Store:
        """Open the pool and bring the schema up to date."""
        pool = await create_pool(
            dsn, min_size=1, max_size=5, max_inactive_connection_lifetime=300
        )
        await apply_migrations(pool)

        return cls(pool)

    async def close(self) -> None:
        """Close the pool, and with it every repository."""
        await self._pool.close()
