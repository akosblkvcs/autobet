"""Who may sign in, and the sessions they hold open."""

import hashlib
import secrets
from datetime import timedelta

from asyncpg import Pool, Record

from autobet.models import Role, SignedIn, User, utcnow

SESSION_DAYS = 30


def _hashed(token: str) -> str:
    """Sessions are stored by hash, so a leaked table hands out no logins."""
    return hashlib.sha256(token.encode()).hexdigest()


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
