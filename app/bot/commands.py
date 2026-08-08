from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats
from loguru import logger


BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="new", description="Новый пост"),
    BotCommand(command="draft", description="Новый черновик"),
    BotCommand(command="plan", description="Контент-план"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Помощь"),
)


async def register_bot_commands(bot: Bot) -> bool:
    """Publish the compact command menu for private-chat users.

    Command registration is UX-only: Telegram/API failures are logged but must not
    prevent the bot from starting or polling.
    """
    try:
        await bot.set_my_commands(
            list(BOT_COMMANDS),
            scope=BotCommandScopeAllPrivateChats(),
        )
    except Exception as exc:
        logger.warning("Bot command menu registration failed: {!r}", exc)
        return False
    logger.info("Bot command menu registered: {} commands", len(BOT_COMMANDS))
    return True
