"""The two routes a sign-in needs: out to the provider, and back.

There is no sign-out. The provider keeps its own session and this client asks
for no consent, so signing out here would be undone by the next page load.
Ending a session for real is either done at the provider, or by an admin
disabling the account.
"""

# pyright: reportUnusedFunction=false

import secrets

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from autobet.web.auth import SignInError, flow, keep_session, start_flow
from autobet.web.context import Context, templates

log = structlog.get_logger(__name__)


def router(context: Context) -> APIRouter:
    """Build the /auth routes."""
    api = APIRouter(prefix="/auth")

    @api.get("/login")
    async def login(request: Request) -> Response:
        response = RedirectResponse("/", status_code=303)
        state, nonce, challenge = start_flow(response, context.settings)

        try:
            response.headers["location"] = await context.provider.authorize_url(
                state, nonce, challenge
            )
        except SignInError as error:
            log.error("sign_in_unavailable", detail=str(error))

            return templates.TemplateResponse(
                request, "forbidden.html", {"reason": str(error)}, status_code=503
            )

        return response

    @api.get("/callback")
    async def callback(request: Request, code: str = "", state: str = "") -> Response:
        try:
            started = flow(request)

            if not code or not secrets.compare_digest(state, started.state):
                raise SignInError("the answer did not match the sign-in")

            subject, email = await context.provider.identify(
                code, started.verifier, started.nonce
            )
        except SignInError as error:
            log.warning("sign_in_failed", detail=str(error))

            return templates.TemplateResponse(
                request, "forbidden.html", {"reason": str(error)}, status_code=403
            )

        admin = email in context.settings.admin_emails
        user = await context.store.users.signed_in(subject, email, admin)

        if user is None:
            log.warning("sign_in_refused", subject=subject)

            return templates.TemplateResponse(
                request,
                "forbidden.html",
                {"reason": "this account is disabled here"},
                status_code=403,
            )

        token = await context.store.users.open_session(user)

        log.info("signed_in", user=user.email, role=user.role)

        response = RedirectResponse("/", status_code=303)
        keep_session(response, token, context.settings)

        return response

    return api
