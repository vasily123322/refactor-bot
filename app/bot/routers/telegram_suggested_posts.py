from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.core.db import AsyncSessionLocal
from app.services.telegram_suggested_posts import TelegramSuggestedPostIngestionService


router = Router(name="telegram-suggested-posts")

_SUGGESTED_POST_MESSAGE = (
    F.suggested_post_info
    | F.suggested_post_approval_failed
    | F.suggested_post_approved
    | F.suggested_post_declined
    | F.suggested_post_paid
    | F.suggested_post_refunded
)


@router.message(_SUGGESTED_POST_MESSAGE)
async def ingest_suggested_post_message(message: Message, bot: Bot) -> None:
    """Own native Suggested Posts ingress without borrowing source-creation UI state."""
    async with AsyncSessionLocal() as session:
        await TelegramSuggestedPostIngestionService(session, bot=bot).ingest(message)


@router.edited_message(F.suggested_post_info)
async def ingest_edited_suggested_post(message: Message, bot: Bot) -> None:
    """Edits reconcile the same Telegram-native Suggested Post identity."""
    async with AsyncSessionLocal() as session:
        await TelegramSuggestedPostIngestionService(session, bot=bot).ingest(message)
