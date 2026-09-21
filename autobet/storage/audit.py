"""The audit log: who changed what, append-only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from asyncpg import Pool

from autobet.storage.rows import Executes, JsonValue


@dataclass(frozen=True, slots=True)
class Entry:
    """One change, as the admin page lists it."""

    at: datetime
    actor: str
    action: str
    entity: str
    entity_id: str


class Audit:
    """Every change an admin made, in rows nobody edits afterwards."""

    def __init__(self, pool: Pool) -> None:
        """Share the store's pool."""
        self._pool = pool

    async def record(
        self,
        actor_id: int,
        action: str,
        entity_id: str = "",
        detail: dict[str, JsonValue] | None = None,
        conn: Executes | None = None,
    ) -> None:
        """Note one change."""
        await (conn or self._pool).execute(
            """
            INSERT INTO audit_log (actor_id, action, entity, entity_id, detail)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            actor_id,
            action,
            action.split(".", 1)[0],
            entity_id,
            json.dumps(detail or {}),
        )

    async def recent(self, limit: int = 20) -> list[Entry]:
        """The newest changes, with the email of whoever made each one."""
        rows = await self._pool.fetch(
            """
            SELECT a.at, a.action, a.entity, a.entity_id,
                   coalesce(u.email, '') AS actor
            FROM audit_log a
            LEFT JOIN users u ON u.id = a.actor_id
            ORDER BY a.at DESC
            LIMIT $1
            """,
            limit,
        )

        return [
            Entry(
                at=row["at"],
                actor=row["actor"],
                action=row["action"],
                entity=row["entity"],
                entity_id=row["entity_id"],
            )
            for row in rows
        ]
