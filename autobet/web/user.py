"""The pages a person owns: their account at the book, their terms, their bets."""

# pyright: reportUnusedFunction=false

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from autobet.books import TIPPMIXPRO
from autobet.models import SignedIn
from autobet.storage import Store
from autobet.web.context import Context, templates, whoever

Field = Annotated[str, Form()]
Blank = Annotated[str, Form()]


def _back(page: str) -> RedirectResponse:
    """Back to the page, so a reload does not repeat the change."""
    return RedirectResponse(page, status_code=303)


def _reason(refused: Exception) -> str:
    """Why a value was refused, as the page says it."""
    if isinstance(refused, ValidationError):
        return str(refused.errors()[0].get("msg", refused))

    return str(refused)


async def _owner(request: Request, store: Store, csrf: str) -> SignedIn | Response:
    """The person making this change, or the response to send instead."""
    session = await whoever(request, store)

    if isinstance(session, Response):
        return session

    if not secrets.compare_digest(session.csrf, csrf):
        return Response("stale form", status_code=403)

    return session


async def _settings(
    request: Request, context: Context, session: SignedIn, error: str = ""
) -> Response:
    """Their terms as they stand, with a reason when a change was refused."""
    store = context.store
    held = await store.users.policy(session.user.id)
    policy = await store.config.policy()

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "held": held,
            "defaults": policy,
            "own": await store.users.stored_policy(session.user.id),
            "error": error,
            "session": session,
        },
        status_code=400 if error else 200,
    )


async def _account(
    request: Request, context: Context, session: SignedIn, error: str = ""
) -> Response:
    """Their bookmaker account, which the page never prints the password of."""
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "account": await context.store.accounts.of(session.user.id, TIPPMIXPRO),
            "error": error,
            "session": session,
        },
        status_code=400 if error else 200,
    )


def router(context: Context) -> APIRouter:
    """Build the routes every signed-in person has for their own bets."""
    api = APIRouter()
    store = context.store

    @api.get("/bets", response_class=HTMLResponse)
    async def bets(request: Request) -> Response:
        session = await whoever(request, store)

        if isinstance(session, Response):
            return session

        return templates.TemplateResponse(
            request,
            "bets.html",
            {
                "rows": await store.bets.recent(50, session.user.id),
                "session": session,
            },
        )

    @api.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> Response:
        session = await whoever(request, store)

        return (
            session
            if isinstance(session, Response)
            else await _settings(request, context, session)
        )

    @api.post("/settings")
    async def set_term(
        request: Request, csrf: Field, key: Field, value: Blank = ""
    ) -> Response:
        who = await _owner(request, store, csrf)

        if isinstance(who, Response):
            return who

        try:
            await store.users.set_policy(who.user.id, key, value or None)
        except (ValidationError, ValueError) as refused:
            return await _settings(request, context, who, _reason(refused))

        return _back("/settings")

    @api.get("/account", response_class=HTMLResponse)
    async def account(request: Request) -> Response:
        session = await whoever(request, store)

        return (
            session
            if isinstance(session, Response)
            else await _account(request, context, session)
        )

    @api.post("/account")
    async def save_account(
        request: Request, csrf: Field, username: Blank = "", password: Blank = ""
    ) -> Response:
        who = await _owner(request, store, csrf)

        if isinstance(who, Response):
            return who

        if not username or not password:
            return await _account(
                request, context, who, "both the username and the password"
            )

        await store.accounts.put(who.user.id, TIPPMIXPRO, username, password)

        return _back("/account")

    @api.post("/account/forget")
    async def forget_account(request: Request, csrf: Field) -> Response:
        who = await _owner(request, store, csrf)

        if isinstance(who, Response):
            return who

        await store.accounts.forget(who.user.id, TIPPMIXPRO)

        return _back("/account")

    return api
