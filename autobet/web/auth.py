"""Signing in through the identity provider, and the session it leaves behind."""

import base64
import hashlib
import json
import secrets
import time
import unicodedata

import httpx2
import structlog
from fastapi import Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ValidationError, field_validator

from autobet.config import Settings
from autobet.models import SignedIn
from autobet.storage import Store
from autobet.storage.users import SESSION_DAYS

log = structlog.get_logger(__name__)

_INVISIBLE = frozenset({"Cc", "Cf"})
SESSION_COOKIE = "autobet_session"
_FLOW_COOKIE = "autobet_flow"
_FLOW_MAX_AGE = 600
_TIMEOUT = 15.0


class SignInError(RuntimeError):
    """The sign-in could not be completed. Carries what to tell the caller."""


class Endpoints(BaseModel):
    """The parts of the provider's discovery document this flow uses."""

    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str


class Tokens(BaseModel):
    """What the token endpoint answers a redeemed code with."""

    access_token: str
    id_token: str


class Claims(BaseModel):
    """The ID token claims this flow is allowed to believe."""

    iss: str
    sub: str
    aud: str | list[str]
    exp: int
    nonce: str = ""


class Profile(BaseModel):
    """Who userinfo says the caller is."""

    sub: str
    email: str = ""
    name: str = ""

    @field_validator("email", "name")
    @classmethod
    def _printable(cls, value: str) -> str:
        """The directory owns these and a terminal prints them; see CLAUDE.md."""
        return "".join(
            one for one in value if unicodedata.category(one) not in _INVISIBLE
        ).strip()


class Flow(BaseModel):
    """The one-time values parked on the browser while it is at the provider."""

    state: str
    nonce: str
    verifier: str


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims(id_token: str) -> Claims:
    """The ID token's claims, checked for shape but not for signature."""
    payload = id_token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)

    try:
        return Claims.model_validate_json(base64.urlsafe_b64decode(padded))
    except (ValidationError, ValueError) as error:
        raise SignInError("the provider sent a token we cannot read") from error


class Provider:
    """The identity provider's endpoints, read from its discovery document."""

    def __init__(self, settings: Settings) -> None:
        """Hold the settings; the network happens on first use."""
        self._settings = settings
        self._found: Endpoints | None = None

    async def _discovered(self, client: httpx2.AsyncClient) -> Endpoints:
        if self._found is not None:
            return self._found

        issuer = self._settings.oidc_issuer
        answer = await client.get(f"{issuer}/.well-known/openid-configuration")
        found = Endpoints.model_validate_json(answer.content)

        for endpoint in (found.token_endpoint, found.userinfo_endpoint):
            if not endpoint.startswith(f"{issuer}/"):
                raise SignInError(f"the provider points {endpoint} off its own host")

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

        return f"{found.authorization_endpoint}?{query}"

    async def identify(self, code: str, verifier: str, nonce: str) -> Profile:
        """Redeem the code and return who the provider says it stands for."""
        settings = self._settings

        async with httpx2.AsyncClient(timeout=_TIMEOUT) as client:
            found = await self._discovered(client)
            answer = await client.post(
                found.token_endpoint,
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

            tokens = Tokens.model_validate_json(answer.content)
            claims = _claims(tokens.id_token)
            audience = claims.aud if isinstance(claims.aud, list) else [claims.aud]

            if claims.iss != settings.oidc_issuer:
                raise SignInError("the token came from another issuer")

            if settings.oidc_client_id not in audience:
                raise SignInError("the token was issued to another client")

            if claims.exp <= int(time.time()):
                raise SignInError("the token had already expired")

            if claims.nonce != nonce:
                raise SignInError("the token answers a different sign-in")

            who = await client.get(
                found.userinfo_endpoint,
                headers={"Authorization": f"Bearer {tokens.access_token}"},
            )
            profile = Profile.model_validate_json(who.content)

        if profile.sub != claims.sub:
            raise SignInError("the profile and the token name different people")

        return profile


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


def flow(request: Request) -> Flow:
    """What the browser was told to remember before it left."""
    parked = request.cookies.get(_FLOW_COOKIE)

    if not parked:
        raise SignInError("the sign-in took too long, or cookies are blocked")

    try:
        return Flow.model_validate_json(parked)
    except ValidationError as error:
        raise SignInError("the sign-in cookie is not one of ours") from error


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
