"""Postgres storage: one pool, and a repository per aggregate."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import structlog
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

log = structlog.get_logger(__name__)

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


async def _create(dsn: str) -> None:
    """Create the database the DSN names, from the server's own `postgres` one."""
    parts = urlsplit(dsn)
    name = parts.path.lstrip("/")

    if not _NAME.match(name):
        raise ValueError(f"refusing to create a database named {name!r}")

    conn = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()

    log.info("database_created", name=name)


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
    async def connect(cls, dsn: str, key: str, *, create_missing: bool = False) -> Store:
        """Open the pool and bring the schema up to date."""
        try:
            pool = await cls._opened(dsn)
        except asyncpg.InvalidCatalogNameError:
            if not create_missing:
                raise

            await _create(dsn)
            pool = await cls._opened(dsn)

        await apply_migrations(pool)

        return cls(pool, key)

    @staticmethod
    async def _opened(dsn: str) -> Pool:
        """The pool every repository shares."""
        return await create_pool(
            dsn, min_size=1, max_size=5, max_inactive_connection_lifetime=300
        )

    async def close(self) -> None:
        """Close the pool, and with it every repository."""
        await self._pool.close()
