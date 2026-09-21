"""The admin page: what the CLI changes, changed from a browser instead."""

# pyright: reportUnusedFunction=false

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ValidationError

from autobet.books import TIPPMIXPRO, Tippmixpro
from autobet.models import SignedIn
from autobet.policy import SECRETS
from autobet.storage import Store
from autobet.storage.rows import JsonValue
from autobet.web.context import Context, admin_only, templates

Field = Annotated[str, Form()]
Blank = Annotated[str, Form()]


def _back() -> RedirectResponse:
    """Back to the page, so a reload does not repeat the change."""
    return RedirectResponse("/admin", status_code=303)


def _reason(refused: Exception) -> str:
    """Why a value was refused, as the page says it."""
    if isinstance(refused, ValidationError):
        return str(refused.errors()[0].get("msg", refused))

    return str(refused)


async def _actor(request: Request, store: Store, csrf: str) -> SignedIn | Response:
    """The admin making this change, or the response to send instead."""
    guarded = await admin_only(request, store)

    if isinstance(guarded, Response):
        return guarded

    if not secrets.compare_digest(guarded.csrf, csrf):
        return Response("stale form", status_code=403)

    return guarded


def _fields(held: BaseModel) -> list[dict[str, JsonValue]]:
    """One row per value: what it is, what it holds, and whether to hide it."""
    return [
        {
            "name": name,
            "value": "" if name in SECRETS else str(getattr(held, name)),
            "secret": name in SECRETS,
            "set": bool(getattr(held, name)),
            "description": field.description or "",
        }
        for name, field in type(held).model_fields.items()
    ]


async def _page(
    request: Request, context: Context, session: SignedIn, error: str = ""
) -> Response:
    """The page as it stands, with a reason when a change was refused."""
    store = context.store
    policy = await store.config.policy()
    integrations = await store.config.integrations()

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "limits": _fields(policy),
            "keys": _fields(integrations),
            "book": _fields(Tippmixpro.model_validate(await store.books.stored())),
            "book_enabled": await store.books.enabled(),
            "channels": await store.archive.channels(),
            "people": await store.users.all(),
            "error": error,
            "session": session,
        },
        status_code=400 if error else 200,
    )


def router(context: Context) -> APIRouter:
    """Build the /admin routes."""
    api = APIRouter(prefix="/admin")
    store = context.store

    @api.get("", response_class=HTMLResponse)
    async def page(request: Request) -> Response:
        guarded = await admin_only(request, store)

        return (
            guarded
            if isinstance(guarded, Response)
            else await _page(request, context, guarded)
        )

    @api.post("/setting")
    async def setting(
        request: Request, csrf: Field, key: Field, value: Blank = ""
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        # A blank field leaves the stored value alone, which is how a secret
        # stays editable on a page that never prints it.
        if not value:
            return _back()

        detail: dict[str, JsonValue] = {} if key in SECRETS else {"value": value}

        try:
            async with store.transaction() as tx:
                await store.config.put(key, value, who.user.id, tx)
                await store.audit.record(who.user.id, "settings.put", key, detail, tx)
        except (ValidationError, ValueError) as refused:
            return await _page(request, context, who, _reason(refused))

        return _back()

    @api.post("/book")
    async def book(
        request: Request, csrf: Field, key: Field, value: Blank = ""
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        try:
            async with store.transaction() as tx:
                await store.books.put(key, value, TIPPMIXPRO, tx)
                await store.audit.record(
                    who.user.id, "bookmakers.put", key, {"value": value}, tx
                )
        except (ValidationError, ValueError) as refused:
            return await _page(request, context, who, _reason(refused))

        return _back()

    @api.post("/book/enable")
    async def book_enable(request: Request, csrf: Field) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        async with store.transaction() as tx:
            await store.books.enable(True, TIPPMIXPRO, tx)
            await store.audit.record(who.user.id, "bookmakers.enable", TIPPMIXPRO, {}, tx)

        return _back()

    @api.post("/channel")
    async def channel(
        request: Request, csrf: Field, chat_id: Field, enabled: Field
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        async with store.transaction() as tx:
            if enabled == "true":
                await store.archive.watch(int(chat_id), tx)
            else:
                await store.archive.unwatch(int(chat_id), tx)

            await store.audit.record(
                who.user.id, "channels.enable", chat_id, {"enabled": enabled}, tx
            )

        return _back()

    @api.post("/user")
    async def user(
        request: Request, csrf: Field, user_id: Field, active: Field
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        if int(user_id) == who.user.id:
            return await _page(
                request, context, who, "disabling yourself would lock you out"
            )

        async with store.transaction() as tx:
            await store.users.set_status(int(user_id), active == "true", tx)
            await store.audit.record(
                who.user.id, "users.status", user_id, {"active": active}, tx
            )

        return _back()

    return api
