"""Signing in through the identity provider, and the session it leaves behind."""

import base64
import hashlib
import json
import secrets
from typing import Any

import httpx2
import structlog
from fastapi import Request
from fastapi.responses import RedirectResponse

from autobet.config import Settings
from autobet.models import SignedIn
from autobet.storage import Store
from autobet.storage.users import SESSION_DAYS

log = structlog.get_logger(__name__)

SESSION_COOKIE = "autobet_session"
_FLOW_COOKIE = "autobet_flow"
_FLOW_MAX_AGE = 600
_TIMEOUT = 15.0


class SignInError(RuntimeError):
    """The sign-in could not be completed. Carries what to tell the caller."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims(id_token: str) -> dict[str, Any]:
    """The ID token's claims."""
    payload = id_token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))

    return decoded


class Provider:
    """The identity provider's endpoints, read from its discovery document."""

    def __init__(self, settings: Settings) -> None:
        """Hold the settings; the network happens on first use."""
        self._settings = settings
        self._found: dict[str, Any] | None = None

    async def _discovered(self, client: httpx2.AsyncClient) -> dict[str, Any]:
        if self._found is not None:
            return self._found

        answer = await client.get(
            f"{self._settings.oidc_issuer}/.well-known/openid-configuration"
        )
        found: dict[str, Any] = answer.json()
        self._found = found

        return found

    async def authorize_url(self, state: str, nonce: str, challenge: str) -> str:
        """Where to send the browser to sign in."""
        if not self._settings.oidc_issuer:
            raise SignInError("no identity provider is configured")

        async with httpx2.AsyncClient(timeout=_TIMEOUT) as client:
            found = await self._discovered(client)

        query = httpx2.QueryParams(
            client_id=self._settings.oidc_client_id,
            redirect_uri=self._settings.oidc_redirect_url,
            response_type="code",
            scope="openid profile email",
            state=state,
            nonce=nonce,
            code_challenge=challenge,
            code_challenge_method="S256",
        )

        return f"{found['authorization_endpoint']}?{query}"

    async def identify(self, code: str, verifier: str, nonce: str) -> tuple[str, str]:
        """Redeem the code and return the subject and email it stands for."""
        settings = self._settings

        async with httpx2.AsyncClient(timeout=_TIMEOUT) as client:
            found = await self._discovered(client)
            answer = await client.post(
                found["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.oidc_redirect_url,
                    "code_verifier": verifier,
                },
                auth=(
                    settings.oidc_client_id,
                    settings.oidc_client_secret.get_secret_value(),
                ),
            )

            if answer.status_code != 200:  # noqa: PLR2004
                raise SignInError(f"the provider refused the code: {answer.text[:120]}")

            tokens: dict[str, Any] = answer.json()
            claims = _claims(str(tokens["id_token"]))

            if claims.get("iss") != settings.oidc_issuer:
                raise SignInError("the token came from another issuer")

            if claims.get("nonce") != nonce:
                raise SignInError("the token answers a different sign-in")

            who = await client.get(
                found["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            profile: dict[str, Any] = who.json()

        subject = str(profile.get("sub") or claims.get("sub") or "")

        if not subject:
            raise SignInError("the provider named no subject")

        return subject, str(profile.get("email") or "")


async def signed_in(request: Request, store: Store) -> SignedIn | None:
    """Who is making this request, or None when nobody is."""
    token = request.cookies.get(SESSION_COOKIE)

    return None if not token else await store.users.session(token)


def start_flow(response: RedirectResponse, settings: Settings) -> tuple[str, str, str]:
    """Mint the one-time values for a sign-in and park them on the browser."""
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())

    response.set_cookie(
        _FLOW_COOKIE,
        json.dumps({"state": state, "nonce": nonce, "verifier": verifier}),
        max_age=_FLOW_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
    )

    return state, nonce, challenge


def flow(request: Request) -> dict[str, str]:
    """What the browser was told to remember before it left."""
    parked = request.cookies.get(_FLOW_COOKIE)

    if not parked:
        raise SignInError("the sign-in took too long, or cookies are blocked")

    remembered: dict[str, str] = json.loads(parked)

    return remembered


def keep_session(response: RedirectResponse, token: str, settings: Settings) -> None:
    """Put the session cookie on the browser and clear the sign-in one."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
    )
    response.delete_cookie(_FLOW_COOKIE)
