"""Who may sign in, the sessions they hold open, and each person's own terms."""

import hashlib
import secrets
from collections.abc import Sequence
from datetime import timedelta

from asyncpg import Pool, Record

from autobet.models import Person, Role, SignedIn, User, utcnow
from autobet.policy import UserPolicy
from autobet.storage.rows import JsonValue

SESSION_DAYS = 30


def _hashed(token: str) -> str:
    """Sessions are stored by hash, so a leaked table hands out no logins."""
    return hashlib.sha256(token.encode()).hexdigest()


def _to_policy(row: Record) -> UserPolicy:
    """One `user_settings` row; a null column means the service decides."""
    return UserPolicy(
        mode=row["mode"],
        paused=row["paused"],
        stake=row["stake"],
        max_odds_drop_percent=row["max_odds_drop_percent"],
    )


def _to_user(row: Record) -> User:
    return User(
        id=row["id"],
        subject=row["subject"] or "",
        email=row["email"] or "",
        role=Role(row["role"]),
    )


class Users:
    """Identities from the provider, and the cookie sessions built on them."""

    def __init__(self, pool: Pool) -> None:
        """Share the store's pool."""
        self._pool = pool

    async def signed_in(self, subject: str, email: str, admin: bool) -> User | None:
        """The user this subject belongs to, creating the row on first sign-in."""
        async with self._pool.acquire() as conn, conn.transaction():
            known = await conn.fetchrow(
                """
                UPDATE users SET email = $2, role = $3, last_login_at = now()
                WHERE subject = $1 AND status = 'active'
                RETURNING id, subject, email, role
                """,
                subject,
                email,
                Role.ADMIN if admin else Role.USER,
            )

            if known is not None:
                return _to_user(known)

            if await conn.fetchval("SELECT 1 FROM users WHERE subject = $1", subject):
                return None

            created = await conn.fetchrow(
                """
                INSERT INTO users (subject, email, role, last_login_at)
                VALUES ($1, $2, $3, now())
                RETURNING id, subject, email, role
                """,
                subject,
                email,
                Role.ADMIN if admin else Role.USER,
            )
            assert created is not None

            return _to_user(created)

    async def all(self) -> list[Person]:
        """Everyone who has ever signed in, newest first."""
        rows = await self._pool.fetch(
            """
            SELECT id, subject, email, role, status, last_login_at
            FROM users ORDER BY created_at DESC
            """
        )

        return [
            Person(
                user=_to_user(row),
                status=row["status"],
                last_login_at=row["last_login_at"],
            )
            for row in rows
        ]

    async def by_id(self, user_id: int) -> User | None:
        """The user this id belongs to, or None."""
        row = await self._pool.fetchrow(
            "SELECT id, subject, email, role FROM users WHERE id = $1", user_id
        )

        return None if row is None else _to_user(row)

    async def matching(self, email: str) -> list[User]:
        """Everyone who signs in with this address."""
        rows = await self._pool.fetch(
            "SELECT id, subject, email, role FROM users WHERE email = $1 ORDER BY id",
            email,
        )

        return [_to_user(row) for row in rows]

    async def policies(self, user_ids: Sequence[int]) -> dict[int, UserPolicy]:
        """Each of these people's terms, defaulted for anyone with no row."""
        rows = await self._pool.fetch(
            """
            SELECT user_id, mode, paused, stake, max_odds_drop_percent
            FROM user_settings WHERE user_id = ANY($1::bigint[])
            """,
            list(user_ids),
        )
        stored = {row["user_id"]: _to_policy(row) for row in rows}

        return {user_id: stored.get(user_id, UserPolicy()) for user_id in user_ids}

    async def policy(self, user_id: int) -> UserPolicy:
        """One person's terms, defaulted when they have never set any."""
        return (await self.policies([user_id]))[user_id]

    async def set_policy(self, user_id: int, key: str, value: JsonValue) -> None:
        """Change one of somebody's terms, after the model has validated the rest."""
        current = (await self.policy(user_id)).model_dump(mode="json")
        terms = UserPolicy.model_validate(current | {key: value})

        await self._pool.execute(
            f"""
            INSERT INTO user_settings (user_id, {key}) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET {key} = EXCLUDED.{key},
                                                updated_at = now()
            """,
            user_id,
            getattr(terms, key),
        )

    async def stored_policy(self, user_id: int) -> set[str]:
        """Which of somebody's terms are their own rather than the service's."""
        row = await self._pool.fetchrow(
            "SELECT stake, max_odds_drop_percent FROM user_settings WHERE user_id = $1",
            user_id,
        )

        if row is None:
            return set()

        return {"mode", "paused"} | {
            name for name in ("stake", "max_odds_drop_percent") if row[name] is not None
        }

    async def set_status(self, user_id: int, active: bool) -> None:
        """Let someone in, or end their access now."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE users SET status = $2 WHERE id = $1",
                user_id,
                "active" if active else "disabled",
            )

            if not active:
                await conn.execute(
                    """
                    UPDATE sessions SET revoked_at = now()
                    WHERE user_id = $1 AND revoked_at IS NULL
                    """,
                    user_id,
                )

    async def open_session(self, user: User) -> str:
        """Start a session for this user and return the cookie value."""
        token = secrets.token_urlsafe(32)

        await self._pool.execute(
            """
            INSERT INTO sessions (token_hash, user_id, csrf, expires_at)
            VALUES ($1, $2, $3, $4)
            """,
            _hashed(token),
            user.id,
            secrets.token_urlsafe(32),
            utcnow() + timedelta(days=SESSION_DAYS),
        )

        return token

    async def session(self, token: str) -> SignedIn | None:
        """Who this cookie belongs to, or None when it is unknown or spent."""
        row = await self._pool.fetchrow(
            """
            SELECT u.id, u.subject, u.email, u.role, s.csrf
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = $1
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND u.status = 'active'
            """,
            _hashed(token),
        )

        return None if row is None else SignedIn(_to_user(row), row["csrf"])
