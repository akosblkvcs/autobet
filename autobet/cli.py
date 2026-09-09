"""Command line entrypoints."""

# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportGeneralTypeIssues=false

import argparse
import asyncio

import structlog

from autobet import markets
from autobet.app import run_service
from autobet.config import Settings, load_settings
from autobet.feed import Feed
from autobet.logging import configure_logging
from autobet.storage import Store
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

    return parser


async def cmd_login(settings: Settings) -> None:
    """Interactively authenticate and write the reusable session file."""
    client = build_client(settings)

    await client.start()

    print(f"Session written to {settings.telegram_session}")

    await client.disconnect()


async def cmd_chats(settings: Settings) -> None:
    """List every chat the session can see, with their ids and names."""
    client = build_client(settings)

    await connect_authorized(client)

    async for dialog in client.iter_dialogs():
        print(f"{dialog.id:>16}  {dialog.name}")

    await client.disconnect()


async def cmd_migrate(settings: Settings) -> None:
    """Apply pending migrations; `run` does this on startup too."""
    store = await Store.connect(settings.database_url)

    await store.close()


async def cmd_markets(settings: Settings) -> None:
    """Harvest the bookmaker's bet types so the parser can name them."""
    feed = Feed(settings)
    await feed.start()
    await feed.indexed()

    vocabulary = await markets.harvest(feed, feed.indexed_events)
    markets.save(vocabulary, settings.market_families)

    print(
        f"{sum(len(names) for names in vocabulary.values())} bet types "
        f"across {len(vocabulary)} sports -> {settings.market_families}"
    )

    await feed.stop()


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
