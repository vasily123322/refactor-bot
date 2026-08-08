from __future__ import annotations

import asyncio

from aiogram.types import BotCommandScopeAllPrivateChats

from app.bot.commands import BOT_COMMANDS, register_bot_commands
from app.bot.routers.commands import _section_kb
from app.core.callbacks import CB


def test_command_menu_is_compact_and_stable() -> None:
    names = [command.command for command in BOT_COMMANDS]
    assert names == ["start", "new", "draft", "plan", "settings", "help"]
    assert len(names) == len(set(names))
    assert all(command.command.islower() for command in BOT_COMMANDS)
    assert all(1 <= len(command.command) <= 32 for command in BOT_COMMANDS)
    assert all(1 <= len(command.description) <= 256 for command in BOT_COMMANDS)


def test_command_registration_uses_private_chat_scope() -> None:
    class _Bot:
        def __init__(self) -> None:
            self.commands = None
            self.scope = None

        async def set_my_commands(self, commands, *, scope):
            self.commands = commands
            self.scope = scope
            return True

    async def run() -> None:
        bot = _Bot()
        assert await register_bot_commands(bot) is True  # type: ignore[arg-type]
        assert [item.command for item in bot.commands] == [
            "start",
            "new",
            "draft",
            "plan",
            "settings",
            "help",
        ]
        assert isinstance(bot.scope, BotCommandScopeAllPrivateChats)

    asyncio.run(run())


def test_command_registration_failure_is_nonfatal() -> None:
    class _Bot:
        async def set_my_commands(self, commands, *, scope):
            raise RuntimeError("telegram unavailable")

    assert asyncio.run(register_bot_commands(_Bot())) is False  # type: ignore[arg-type]


def test_section_shortcuts_return_to_same_navigation_shell() -> None:
    keyboard = _section_kb(label="Open", callback_data=str(CB.CP_OPEN))
    callbacks = [
        str(button.callback_data)
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert callbacks == [CB.CP_OPEN.value, CB.GM_GLOBAL_MENU.value]
