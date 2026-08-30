"""Schema migrations: numbered ``.sql`` files, applied once each, in order.

There is no migration framework and no autogeneration. To change the schema,
add the next numbered file; it runs on the next start. Postgres DDL is
transactional, so a file lands whole or not at all.
"""

from pathlib import Path

import structlog
from asyncpg import Pool

log = structlog.get_logger(__name__)

MIGRATIONS = Path(__file__).parent / "migrations"


async def apply_migrations(pool: Pool) -> None:
    """Apply every migration file that has not been applied yet."""
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS applied_migrations (name text PRIMARY KEY)"
    )
    rows = await pool.fetch("SELECT name FROM applied_migrations")
    applied = {row["name"] for row in rows}

    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in applied:
            continue
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute(
                "INSERT INTO applied_migrations (name) VALUES ($1)", path.name
            )
        log.info("migration_applied", name=path.name)
