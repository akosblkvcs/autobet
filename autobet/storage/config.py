"""The settings table: what an admin changes without a deploy."""

from __future__ import annotations

import json

from asyncpg import Pool
from pydantic import BaseModel

from autobet.policy import Integrations, Policy
from autobet.storage.rows import Executes, JsonValue

# Every model over this one table. A key belongs to exactly one of them, and
# that model is what validates it.
MODELS: tuple[type[BaseModel], ...] = (Policy, Integrations)


def owner(key: str) -> type[BaseModel]:
    """The model a key belongs to."""
    for model in MODELS:
        if key in model.model_fields:
            return model

    raise ValueError(f"unknown setting: {key}")


class Config:
    """Stored configuration, defaulted in code so an empty table still runs."""

    def __init__(self, pool: Pool) -> None:
        """Wrap an open pool; :class:`autobet.storage.Store` owns it."""
        self._pool = pool

    async def stored(self) -> dict[str, JsonValue]:
        """Only what somebody has set, so a page can say what was changed."""
        rows = await self._pool.fetch("SELECT key, value FROM settings ORDER BY key")
        known = {name for model in MODELS for name in model.model_fields}

        return {
            row["key"]: json.loads(row["value"]) for row in rows if row["key"] in known
        }

    async def policy(self) -> Policy:
        """The limits in force: what the table holds over the defaults in code."""
        return Policy.model_validate(await self._for(Policy))

    async def integrations(self) -> Integrations:
        """The keys the service connects with, read once at startup."""
        return Integrations.model_validate(await self._for(Integrations))

    async def put(
        self,
        key: str,
        value: JsonValue,
        actor_id: int | None = None,
        conn: Executes | None = None,
    ) -> None:
        """Store one value, after the owning model has validated the result."""
        model = owner(key)
        current = model.model_validate(await self._for(model)).model_dump(mode="json")
        validated = model.model_validate(current | {key: value})

        await (conn or self._pool).execute(
            """
            INSERT INTO settings (key, value, updated_by) VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                            updated_at = now(),
                                            updated_by = excluded.updated_by
            """,
            key,
            json.dumps(validated.model_dump(mode="json")[key]),
            actor_id,
        )

    async def _for(self, model: type[BaseModel]) -> dict[str, JsonValue]:
        """The stored values this model owns."""
        stored = await self.stored()

        return {key: stored[key] for key in model.model_fields if key in stored}
