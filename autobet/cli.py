"""Command line entrypoints."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportGeneralTypeIssues=false, reportArgumentType=false

import argparse
import asyncio

import structlog
from telethon import TelegramClient

from autobet.app import run_service
from autobet.config import Settings, load_settings
from autobet.logging import configure_logging
from autobet.sources.telegram import SessionError, build_client, connect_authorized
from autobet.storage import MessageStore

log = structlog.get_logger(__name__)


async def _connected(settings: Settings) -> TelegramClient:
    """Build and connect a client for the one-shot commands."""
    client = build_client(settings)

    await connect_authorized(client)

    return client


async def cmd_migrate(settings: Settings) -> None:
    """Apply pending migrations; `run` does this on startup too."""
    store = await MessageStore.connect(settings.database_url)

    await store.close()


async def cmd_login(settings: Settings) -> None:
    """Interactively authenticate and write the reusable session file."""
    client = build_client(settings)

    await client.start()

    print(f"Session written to {settings.telegram_session}")
    await client.disconnect()


async def cmd_chats(settings: Settings, search: str | None) -> None:
    """List every dialog with its numeric id, for TELEGRAM_SOURCE_CHAT_IDS."""
    client = await _connected(settings)

    async for dialog in client.iter_dialogs():
        name = dialog.name or ""

        if search and search.casefold() not in name.casefold() and not dialog.is_channel:
            continue

        print(f"{dialog.id:>16}  {name}")

    await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="autobet",
        description="Tip ingestion and automated bet placement.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run every configured source and the control plane")
    sub.add_parser("login", help="interactively create the Telegram session file")
    sub.add_parser("migrate", help="apply pending schema migrations")

    chats = sub.add_parser("chats", help="list chat ids visible to this account")
    chats.add_argument(
        "--search", help="case-insensitive substring filter on the chat name"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected command."""
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(
        settings.log_level, json_output=settings.environment == "production"
    )

    try:
        match args.command:  # pyright: ignore[reportMatchNotExhaustive]
            case "run":
                asyncio.run(run_service(settings))
            case "login":
                asyncio.run(cmd_login(settings))
            case "migrate":
                asyncio.run(cmd_migrate(settings))
            case "chats":
                asyncio.run(cmd_chats(settings, args.search))
    except SessionError as error:
        log.error(
            "session_dead",
            reason=str(error),
            session=str(settings.telegram_session),
            fix="delete it and run `make login`",
        )
        return 1

    return 0
