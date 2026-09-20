"""The bookmakers table: one row per book, its configuration as jsonb."""

from __future__ import annotations

import json
from typing import Any

from asyncpg import Pool

from autobet.books import TIPPMIXPRO, Tippmixpro


class Books:
    """Each book's configuration, validated by the adapter that reads it."""

    def __init__(self, pool: Pool) -> None:
        """Wrap an open pool; :class:`autobet.storage.Store` owns it."""
        self._pool = pool

    async def stored(self, slug: str = TIPPMIXPRO) -> dict[str, Any]:
        """Only the keys somebody has set, so a page can say what was changed."""
        row = await self._pool.fetchrow(
            "SELECT config FROM bookmakers WHERE slug = $1", slug
        )

        if row is None:
            raise ValueError(f"no bookmaker row for {slug}")

        config: dict[str, Any] = json.loads(row["config"])

        return {key: config[key] for key in Tippmixpro.model_fields if key in config}

    async def config(self, slug: str = TIPPMIXPRO) -> Tippmixpro:
        """The book's endpoints, defaulted in code so an empty row still loads."""
        return Tippmixpro.model_validate(await self.stored(slug))

    async def put(self, key: str, value: Any, slug: str = TIPPMIXPRO) -> None:
        """Store one endpoint, after the adapter's model has validated the result."""
        config = Tippmixpro.model_validate(await self.stored(slug) | {key: value})

        await self._pool.execute(
            "UPDATE bookmakers SET config = $2::jsonb WHERE slug = $1",
            slug,
            json.dumps(config.model_dump(mode="json")),
        )
