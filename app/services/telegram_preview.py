from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.content import PostDocument
from app.services.content import LegacyPayloadError, legacy_payload_from_document
from app.services.posting import PostingService


class TelegramPreviewError(RuntimeError):
    pass


class TelegramPreviewService:
    """Send an exact classic preview through the production Bot API renderer."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        posting_factory: Callable[..., PostingService] = PostingService,
    ):
        self.bot = bot
        self.session_factory = session_factory
        self.posting_factory = posting_factory

    async def send(
        self,
        *,
        tg_user_id: int,
        document: PostDocument,
        replace_message_ids: list[int] | None = None,
    ) -> list[int]:
        try:
            payload = legacy_payload_from_document(document)
        except LegacyPayloadError as exc:
            raise TelegramPreviewError(str(exc)) from exc

        posting = self.posting_factory(self.bot, self.session_factory)
        message_ids = await posting.send_now(int(tg_user_id), payload)
        if not message_ids:
            raise TelegramPreviewError("Telegram preview could not be delivered")

        # Delete stale preview only after a complete new render was delivered.
        # Always bind deletion to the authenticated user's own chat; clients never
        # provide a target chat id.
        new_ids = {int(value) for value in message_ids}
        for old_message_id in replace_message_ids or []:
            old_id = int(old_message_id)
            if old_id in new_ids:
                continue
            with suppress(Exception):
                await self.bot.delete_message(
                    chat_id=int(tg_user_id),
                    message_id=old_id,
                )

        return [int(value) for value in message_ids]
