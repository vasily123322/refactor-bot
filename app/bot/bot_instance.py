from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.core.config import settings
from app.core.redaction import redact_secret_text


class RedactingBot(Bot):
    """Main bot client that never sends raw credential-shaped text."""

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        *args: Any,
        **kwargs: Any,
    ):
        return await super().send_message(
            chat_id,
            redact_secret_text(text),
            *args,
            **kwargs,
        )


bot = RedactingBot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
