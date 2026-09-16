from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter
from aiogram.types import Message

from app.core.db import AsyncSessionLocal
from app.services.telegram_channel_dms import (
    TelegramChannelDMIngestionService,
    is_ordinary_channel_dm,
)


class OrdinaryChannelDMFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return is_ordinary_channel_dm(message)


router = Router(name="telegram-channel-dms")
router.message.filter(F.chat.is_direct_messages)
router.edited_message.filter(F.chat.is_direct_messages)


@router.message(OrdinaryChannelDMFilter())
async def ingest_channel_dm_message(message: Message, bot: Bot) -> None:
    async with AsyncSessionLocal() as session:
        await TelegramChannelDMIngestionService(session, bot=bot).ingest(message)


@router.edited_message(OrdinaryChannelDMFilter())
async def ingest_edited_channel_dm(message: Message, bot: Bot) -> None:
    async with AsyncSessionLocal() as session:
        await TelegramChannelDMIngestionService(session, bot=bot).ingest(message)
