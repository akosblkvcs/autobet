"""The admin page: what the CLI changes, changed from a browser instead."""

# pyright: reportUnusedFunction=false

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

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


async def _page(
    request: Request, context: Context, session: SignedIn, error: str = ""
) -> Response:
    """The page as it stands, with a reason when a change was refused."""
    store = context.store

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "policy": await store.config.policy(),
            "integrations": await store.config.integrations(),
            "secrets": sorted(SECRETS),
            "book": await store.books.stored(),
            "book_fields": list(Tippmixpro.model_fields),
            "book_enabled": await store.books.enabled(),
            "channels": await store.archive.channels(),
            "people": await store.users.all(),
            "entries": await store.audit.recent(),
            "error": error,
            "session": session,
        },
        status_code=400 if error else 200,
    )


async def _settings(
    store: Store, who: SignedIn, sent: dict[str, str], logged: bool
) -> str:
    """Store the settings that changed, and say why if one was refused."""
    held = (await store.config.policy()).model_dump(mode="json")

    for key, value in sent.items():
        if not value or value == str(held.get(key, "")):
            continue

        try:
            await store.config.put(key, value, who.user.id)
        except ValidationError as refused:
            return _reason(refused)

        detail: dict[str, JsonValue] = {"value": value} if logged else {}
        await store.audit.record(who.user.id, "settings.put", "settings", key, detail)

    return ""


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

    @api.post("/limits")
    async def limits(
        request: Request,
        csrf: Field,
        stake: Field,
        max_odds_drop_percent: Field,
        mismatch_rise_percent: Field,
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        error = await _settings(
            store,
            who,
            {
                "stake": stake,
                "max_odds_drop_percent": max_odds_drop_percent,
                "mismatch_rise_percent": mismatch_rise_percent,
            },
            logged=True,
        )

        return await _page(request, context, who, error) if error else _back()

    @api.post("/keys")
    async def keys(
        request: Request,
        csrf: Field,
        telegram_api_id: Blank = "",
        telegram_api_hash: Blank = "",
        claude_api_key: Blank = "",
    ) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        # A blank field leaves the stored value alone, which is how a secret
        # stays editable on a page that never prints it.
        error = await _settings(
            store,
            who,
            {
                "telegram_api_id": telegram_api_id,
                "telegram_api_hash": telegram_api_hash,
                "claude_api_key": claude_api_key,
            },
            logged=False,
        )

        return await _page(request, context, who, error) if error else _back()

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

    @api.post("/book/state")
    async def book_state(request: Request, csrf: Field, enabled: Field) -> Response:
        who = await _actor(request, store, csrf)

        if isinstance(who, Response):
            return who

        await store.books.enable(enabled == "true")
        await store.audit.record(
            who.user.id, "book.enable", "bookmakers", "", {"enabled": enabled}
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
