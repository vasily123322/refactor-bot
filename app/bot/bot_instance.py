from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.core.config import settings
from app.core.redaction import redact_secret_text


class RedactingBot(Bot):
    """Main bot client that redacts credential-shaped text before Telegram API calls."""

    async def __call__(self, method, request_timeout=None):
        for field in ("text", "caption"):
            value = getattr(method, field, None)
            if isinstance(value, str):
                setattr(method, field, redact_secret_text(value))
        return await super().__call__(method, request_timeout=request_timeout)


bot = RedactingBot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
