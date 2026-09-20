"""Command line entrypoints."""

# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportGeneralTypeIssues=false

import argparse
import asyncio

import structlog

from autobet import markets
from autobet.app import run_service
from autobet.books import Tippmixpro
from autobet.config import Settings, load_settings
from autobet.connection import connected
from autobet.index import Index
from autobet.logging import configure_logging
from autobet.storage import Store
from autobet.storage.config import MODELS
from autobet.telegram import SessionError, build_client, connect_authorized

log = structlog.get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run")
    sub.add_parser("login")
    sub.add_parser("chats")
    sub.add_parser("migrate")
    sub.add_parser("markets")
    sub.add_parser("index")

    config = sub.add_parser("config")
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?")

    book = sub.add_parser("book")
    book.add_argument("key", nargs="?")
    book.add_argument("value", nargs="?")

    channel = sub.add_parser("channel")
    channel.add_argument("action", nargs="?", choices=("enable", "disable"))
    channel.add_argument("chat_id", nargs="?", type=int)

    return parser


async def cmd_login(settings: Settings) -> None:
    """Interactively authenticate and write the reusable session file."""
    store = await Store.connect(settings.database_url)
    client = build_client(settings, await store.config.integrations())

    await store.close()
    await client.start()

    print(f"Session written to {settings.telegram_session}")

    await client.disconnect()


async def cmd_chats(settings: Settings) -> None:
    """List every chat the session can see, with their ids and names."""
    store = await Store.connect(settings.database_url)
    client = build_client(settings, await store.config.integrations())

    await store.close()
    await connect_authorized(client)

    async for dialog in client.iter_dialogs():
        print(f"{dialog.id:>16}  {dialog.name}")

    await client.disconnect()


async def cmd_migrate(settings: Settings) -> None:
    """Apply pending migrations; `run` does this on startup too."""
    store = await Store.connect(settings.database_url)

    await store.close()


async def cmd_config(settings: Settings, key: str | None, value: str | None) -> None:
    """Show the stored limits, or set one of them."""
    store = await Store.connect(settings.database_url)

    if key is not None and value is not None:
        await store.config.put(key, value)

    stored = await store.config.stored()

    for model in MODELS:
        values = model.model_validate(
            {key: stored[key] for key in model.model_fields if key in stored}
        )

        for name, field in model.model_fields.items():
            source = "stored" if name in stored else "default"
            value = str(getattr(values, name))
            print(f"{name:<22} {value:<14} {source:<8} {field.description}")

    await store.close()


async def cmd_book(settings: Settings, key: str | None, value: str | None) -> None:
    """Show the book's endpoints, or set one of them."""
    store = await Store.connect(settings.database_url)

    if key is not None and value is not None:
        await store.books.put(key, value)

    stored = await store.books.stored()
    config = await store.books.config()

    for name, field in Tippmixpro.model_fields.items():
        source = "stored" if name in stored else "default"
        print(f"{name:<10} {getattr(config, name):<44} {source:<8} {field.description}")

    await store.close()


async def cmd_channel(
    settings: Settings, action: str | None, chat_id: int | None
) -> None:
    """List the watched chats, or enable and disable one."""
    store = await Store.connect(settings.database_url)

    if action is not None and chat_id is not None:
        if action == "enable":
            await store.archive.watch(chat_id)
        else:
            await store.archive.unwatch(chat_id)

    for watched_id, title, enabled in await store.archive.channels():
        print(f"{watched_id:>16}  {'on ' if enabled else 'off'}  {title}")

    await store.close()


async def cmd_index(settings: Settings) -> None:
    """Walk the bookmaker's board and store the event index."""
    store = await Store.connect(settings.database_url)

    print(f"{await Index(store).rebuild()} events indexed")

    await store.close()


async def cmd_markets(settings: Settings) -> None:
    """Harvest the bookmaker's bet types so the parser can name them."""
    store = await Store.connect(settings.database_url)
    events = await store.events.all()

    async with connected(await store.books.config()) as connection:
        vocabulary = await markets.harvest(connection, events)

    markets.save(vocabulary, settings.market_families)

    print(
        f"{sum(len(names) for names in vocabulary.values())} bet types "
        f"across {len(vocabulary)} sports -> {settings.market_families}"
    )

    await store.close()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected command."""
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(
        settings.log_level, json_output=settings.environment == "production"
    )

    try:
        match args.command:
            case "run":
                asyncio.run(run_service(settings))
            case "login":
                asyncio.run(cmd_login(settings))
            case "chats":
                asyncio.run(cmd_chats(settings))
            case "migrate":
                asyncio.run(cmd_migrate(settings))
            case "markets":
                asyncio.run(cmd_markets(settings))
            case "index":
                asyncio.run(cmd_index(settings))
            case "config":
                asyncio.run(cmd_config(settings, args.key, args.value))
            case "book":
                asyncio.run(cmd_book(settings, args.key, args.value))
            case "channel":
                asyncio.run(cmd_channel(settings, args.action, args.chat_id))
            case _:
                pass
    except SessionError as error:
        log.error(
            "session_unusable",
            reason=str(error),
            session=str(settings.telegram_session),
            fix=error.fix,
        )

        return 1

    return 0
