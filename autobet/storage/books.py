"""The bookmakers table: one row per book, its configuration as jsonb."""

from __future__ import annotations

import json

from asyncpg import Pool, Record

from autobet.books import TIPPMIXPRO, Tippmixpro
from autobet.storage.rows import Executes, JsonValue


def _known(row: Record) -> dict[str, JsonValue]:
    """The keys of this row's config that the adapter's model has."""
    config: dict[str, JsonValue] = json.loads(row["config"])

    return {key: config[key] for key in Tippmixpro.model_fields if key in config}


class Books:
    """Each book's configuration, validated by the adapter that reads it."""

    def __init__(self, pool: Pool) -> None:
        """Wrap an open pool; :class:`autobet.storage.Store` owns it."""
        self._pool = pool

    async def stored(
        self, slug: str = TIPPMIXPRO, conn: Executes | None = None
    ) -> dict[str, JsonValue]:
        """Only the keys somebody has set, so a page can say what was changed."""
        return _known(await self._row(slug, conn))

    async def config(self, slug: str = TIPPMIXPRO) -> Tippmixpro:
        """The endpoints of a book that is ready to be used."""
        row = await self._row(slug)

        if not row["enabled"]:
            raise ValueError(
                f"{slug} is not configured: fill it in with `python -m autobet book "
                f"<key> <value>`, then `python -m autobet book --enable`"
            )

        return Tippmixpro.model_validate(_known(row))

    async def enable(
        self, enabled: bool, slug: str = TIPPMIXPRO, conn: Executes | None = None
    ) -> None:
        """Let the book be used, or stop it being used."""
        await (conn or self._pool).execute(
            "UPDATE bookmakers SET enabled = $2 WHERE slug = $1", slug, enabled
        )

    async def enabled(self, slug: str = TIPPMIXPRO) -> bool:
        """Whether this book may be used."""
        return bool((await self._row(slug))["enabled"])

    async def put(
        self,
        key: str,
        value: JsonValue,
        slug: str = TIPPMIXPRO,
        conn: Executes | None = None,
    ) -> None:
        """Store one endpoint, after the adapter's model has validated the result."""
        config = Tippmixpro.model_validate(await self.stored(slug, conn) | {key: value})

        await (conn or self._pool).execute(
            "UPDATE bookmakers SET config = $2::jsonb WHERE slug = $1",
            slug,
            json.dumps(config.model_dump(mode="json")),
        )

    async def _row(self, slug: str, conn: Executes | None = None) -> Record:
        """The book's row, or a reason there is not one."""
        row = await (conn or self._pool).fetchrow(
            "SELECT config, enabled FROM bookmakers WHERE slug = $1", slug
        )

        if row is None:
            raise ValueError(f"no bookmaker row for {slug}")

        return row
