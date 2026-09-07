"""The Telegram channels we watch, as a stream of messages."""

# pyright: reportMissingTypeStubs=false, reportGeneralTypeIssues=false
# pyright: reportUnknownMemberType=false

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

import structlog
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
)

from autobet.config import Settings
from autobet.models import IncomingMessage, utcnow

log = structlog.get_logger(__name__)


class SessionError(RuntimeError):
    """The session is unusable. Carries the reason and the one-line fix."""

    def __init__(self, reason: str, fix: str) -> None:
        """Store the fix alongside the reason; the caller decides how to show it."""
        super().__init__(reason)
        self.fix = fix


_DEAD_SESSION_ERRORS = (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
)


def build_client(settings: Settings) -> TelegramClient:
    """Construct a Telethon client pointed at the persistent session file."""
    settings.telegram_session.parent.mkdir(parents=True, exist_ok=True)

    return TelegramClient(
        str(settings.telegram_session),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )


async def connect_authorized(client: TelegramClient) -> None:
    """Connect with the stored session, never prompting for input.

    Raises:
        SessionError: The session is dead or not logged in.
    """
    try:
        await client.connect()
    except _DEAD_SESSION_ERRORS as error:
        raise SessionError(
            type(error).__name__, "delete it and run `make login`"
        ) from error
    except sqlite3.OperationalError as error:
        raise SessionError(str(error), "chown to appuser; run one client only") from error

    if not await client.is_user_authorized():
        raise SessionError("not authorized", "run `make login`")


def message_id(chat_id: int, telegram_message_id: int) -> str:
    """Build the archive id; Telegram ids are only unique within a chat."""
    return f"{chat_id}:{telegram_message_id}"


class TelegramSource:
    """Yields messages from the watched chats, in arrival order."""

    name = "telegram"

    def __init__(self, settings: Settings) -> None:
        """Build the client and subscribe; no network happens until ``start``."""
        self._settings = settings
        chats = list(settings.telegram_source_chat_ids)
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._channels: tuple[str, ...] = ()
        self._client = build_client(settings)
        settings.telegram_media_dir.mkdir(parents=True, exist_ok=True)

        if chats:
            self._client.add_event_handler(
                self._on_message, events.NewMessage(chats=chats)
            )

    async def start(self) -> None:
        """Connect with the stored session, name the watched chats, and listen."""
        await connect_authorized(self._client)
        await self._client.get_dialogs()

        self._channels = tuple(
            [
                await self._name_of(chat)
                for chat in self._settings.telegram_source_chat_ids
            ]
        )
        log.info("telegram_connected", watching=self._channels)

    async def _name_of(self, chat: int) -> str:
        """A human label for a watched chat, falling back to its id."""
        try:
            entity = await self._client.get_entity(chat)
        except (ValueError, TypeError):
            log.warning("chat_unnamed", chat=chat)

            return str(chat)

        name: str | None = getattr(entity, "title", None) or getattr(
            entity, "username", None
        )

        return name or str(chat)

    @property
    def channels(self) -> tuple[str, ...]:
        """Titles of the watched chats, empty until ``start`` has resolved them."""
        return self._channels

    async def _on_message(self, event: Any) -> None:
        received_at = utcnow()
        chat_id = int(event.chat_id)
        media_path = (
            await event.message.download_media(
                file=str(
                    self._settings.telegram_media_dir
                    / f"{chat_id}_{event.message.id}.jpg"
                )
            )
            if event.message.photo
            else None
        )

        self._queue.put_nowait(
            IncomingMessage(
                external_id=message_id(chat_id, int(event.message.id)),
                channel=getattr(event.chat, "title", None) or str(event.chat_id),
                sent_at=event.message.date,
                received_at=received_at,
                text=event.message.message or "",
                media_kind=(
                    type(event.message.media).__name__ if event.message.media else None
                ),
                media_path=media_path,
            )
        )

    async def messages(self) -> AsyncIterator[IncomingMessage]:
        """Iterate messages as they arrive, forever."""
        while True:
            yield await self._queue.get()

    def healthy(self) -> bool:
        """Whether the MTProto connection is up."""
        return bool(self._client.is_connected())

    async def stop(self) -> None:
        """Close the MTProto connection."""
        await self._client.disconnect()
