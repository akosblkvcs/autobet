"""The Telegram channel as a tip source."""

# pyright: reportMissingTypeStubs=false, reportGeneralTypeIssues=false

import asyncio
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
    """The session is unusable; the message is the reason, nothing more."""


_DEAD_SESSION = (
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
    except _DEAD_SESSION as error:
        raise SessionError(type(error).__name__) from error

    if not await client.is_user_authorized():
        raise SessionError("not authorized")


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
        self._client = build_client(settings)
        settings.media_dir.mkdir(parents=True, exist_ok=True)

        if chats:
            self._client.add_event_handler(
                self._on_message, events.NewMessage(chats=chats)
            )

    async def start(self) -> None:
        """Connect with the stored session and begin receiving updates."""
        await connect_authorized(self._client)
        log.info("telegram_connected", watching=self._settings.telegram_source_chat_ids)

    async def _on_message(self, event: Any) -> None:
        received_at = utcnow()
        chat_id = int(event.chat_id)
        media_path = (
            await event.message.download_media(
                file=str(self._settings.media_dir / f"{chat_id}_{event.message.id}.jpg")
            )
            if event.message.photo
            else None
        )

        self._queue.put_nowait(
            IncomingMessage(
                source=self.name,
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
