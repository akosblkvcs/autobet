"""The token check guarding every page that is not the liveness probe."""

import secrets

from fastapi import Request

from autobet.config import Settings

TOKEN_COOKIE = "autobet_token"
TOKEN_COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def presented_token(request: Request, settings: Settings) -> str | None:
    """Return the caller's token when it matches; an unset token locks the page."""
    given = request.cookies.get(TOKEN_COOKIE) or request.query_params.get("token")
    expected = settings.autobet_token

    if not expected or not given:
        return None

    return given if secrets.compare_digest(given, expected) else None
