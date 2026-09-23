"""The admin section."""

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


def _back(tab: str) -> RedirectResponse:
    """Back to the sub-tab, so a reload does not repeat the change."""
    return RedirectResponse(f"/admin/{tab}", status_code=303)


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
            "switch": isinstance(getattr(held, name), bool),
            "description": field.description or "",
        }
        for name, field in type(held).model_fields.items()
    ]


async def _settings(
    request: Request, context: Context, session: SignedIn, error: str = ""
) -> Response:
    """The limits and the keys, with a reason when a change was refused."""
    store = context.store

    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "limits": _fields(await store.config.policy()),
            "keys": _fields(await store.config.integrations()),
            "error": error,
            "session": session,
        },
        status_code=400 if error else 200,
    )


async def _book(
    request: Request, context: Context, session: SignedIn, error: str = ""
) -> Response:
    """The book's endpoints, and whether it is armed."""
    store = context.store

    return templates.TemplateResponse(
        request,
        "admin_book.html",
        {
            "book": _fields(Tippmixpro.model_validate(await store.books.stored())),
            "book_enabled": await store.books.enabled(),
            "error": error,
            "session": session,
        },
        status_code=400 if error else 200,
    )


async def _users(
    request: Request, context: Context, session: SignedIn, error: str = ""
) -> Response:
    """Everyone who has signed in, and whether they still may."""
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "people": await context.store.users.all(),
            "error": error,
            "session": session,
        },
        status_code=400 if error else 200,
    )


async def _channels(
    request: Request, context: Context, session: SignedIn, error: str = ""
) -> Response:
    """The watched chats, enabled or not."""
    return templates.TemplateResponse(
        request,
        "admin_channels.html",
        {
            "channels": await context.store.archive.channels(),
            "error": error,
            "session": session,
        },
        status_code=400 if error else 200,
    )


def router(context: Context) -> APIRouter:
    """Build the /admin routes."""
    api = APIRouter(prefix="/admin")
    store = context.store

    @api.get("")
    async def page(request: Request) -> Response:
        guarded = await admin_only(request, store)

        return guarded if isinstance(guarded, Response) else _back("activity")

    @api.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> Response:
        guarded = await admin_only(request, store)

        return (
            guarded
            if isinstance(guarded, Response)
            else await _settings(request, context, guarded)
        )

    @api.get("/books", response_class=HTMLResponse)
    async def book_page(request: Request) -> Response:
        guarded = await admin_only(request, store)

        return (
            guarded
            if isinstance(guarded, Response)
            else await _book(request, context, guarded)
        )

    @api.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request) -> Response:
        guarded = await admin_only(request, store)

        return (
            guarded
            if isinstance(guarded, Response)
            else await _users(request, context, guarded)
        )

    @api.get("/channels", response_class=HTMLResponse)
    async def channels_page(request: Request) -> Response:
        guarded = await admin_only(request, store)

        return (
            guarded
            if isinstance(guarded, Response)
            else await _channels(request, context, guarded)
        )

    @api.get("/activity", response_class=HTMLResponse)
    async def everyones(request: Request) -> Response:
        guarded = await admin_only(request, store)

        if isinstance(guarded, Response):
            return guarded

        return templates.TemplateResponse(
            request,
            "all_bets.html",
            {"rows": await store.bets.recent(50), "session": guarded},
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
            return _back("settings")

        try:
            await store.config.put(key, value, who.user.id)
        except (ValidationError, ValueError) as refused:
            return await _settings(request, context, who, _reason(refused))

        return _back("settings")

    @api.post("/books")
    async def book(
        request: Request, csrf: Field, key: Field, value: Blank = ""
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        try:
            await store.books.put(key, value)
        except (ValidationError, ValueError) as refused:
            return await _book(request, context, who, _reason(refused))

        return _back("books")

    @api.post("/books/enable")
    async def book_enable(request: Request, csrf: Field) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        await store.books.enable(True)

        return _back("books")

    @api.post("/channel")
    async def channel(
        request: Request, csrf: Field, chat_id: Field, enabled: Field
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        if enabled == "true":
            await store.archive.watch(int(chat_id))
        else:
            await store.archive.unwatch(int(chat_id))

        return _back("channels")

    @api.post("/user")
    async def user(
        request: Request, csrf: Field, user_id: Field, active: Field
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        if int(user_id) == who.user.id:
            return await _users(
                request, context, who, "disabling yourself would lock you out"
            )

        await store.users.set_status(int(user_id), active == "true")

        return _back("users")

    return api
