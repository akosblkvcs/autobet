"""The admin page: what the CLI changes, changed from a browser instead."""

# pyright: reportUnusedFunction=false

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ValidationError

from autobet.books import Tippmixpro
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

        try:
            await store.config.put(key, value, who.user.id)
        except (ValidationError, ValueError) as refused:
            return await _page(request, context, who, _reason(refused))

        detail: dict[str, JsonValue] = {} if key in SECRETS else {"value": value}
        await store.audit.record(who.user.id, "settings.put", "settings", key, detail)

        return _back()

    @api.post("/book")
    async def book(
        request: Request, csrf: Field, key: Field, value: Blank = ""
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        try:
            await store.books.put(key, value)
        except (ValidationError, ValueError) as refused:
            return await _page(request, context, who, _reason(refused))

        await store.audit.record(
            who.user.id, "book.put", "bookmakers", key, {"value": value}
        )

        return _back()

    @api.post("/channel")
    async def channel(
        request: Request, csrf: Field, chat_id: Field, enabled: Field
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        watching = enabled == "true"
        await (
            store.archive.watch(int(chat_id))
            if watching
            else store.archive.unwatch(int(chat_id))
        )
        await store.audit.record(
            who.user.id, "channel.enable", "channels", chat_id, {"enabled": enabled}
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

        await store.users.set_status(int(user_id), active == "true")
        await store.audit.record(
            who.user.id, "user.status", "users", user_id, {"active": active}
        )

        return _back()

    return api
