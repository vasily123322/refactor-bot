from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.core.db import AsyncSessionLocal
from app.services.telegram_channel_dms import TelegramChannelDMIngestionService


router = Router(name="telegram-channel-dms")
router.message.filter(F.chat.is_direct_messages == True)
router.edited_message.filter(F.chat.is_direct_messages == True)


@router.message()
async def ingest_channel_dm_message(message: Message, bot: Bot) -> None:
    async with AsyncSessionLocal() as session:
        await TelegramChannelDMIngestionService(session, bot=bot).ingest(message)


@router.edited_message()
async def ingest_edited_channel_dm(message: Message, bot: Bot) -> None:
    async with AsyncSessionLocal() as session:
        await TelegramChannelDMIngestionService(session, bot=bot).ingest(message)
