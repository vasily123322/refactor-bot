from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.logging import with_context_logging
from app.services.admin_remove_allrepeat import AdminRemoveAllRepeatService


router = Router()
router.message.filter(F.chat.type == "private")


@router.message(Command("remove_allrepeat"))
@with_context_logging
async def cmd_remove_allrepeat_guarded(message: Message):
    try:
        admin_id = getattr(settings, "admin_user_id", None)
        if not admin_id or int(message.from_user.id) != int(admin_id):
            return await message.reply("Недоступно")
    except Exception:
        return await message.reply("Недоступно")

    try:
        async with AsyncSessionLocal() as session:
            result = await AdminRemoveAllRepeatService(session).execute()
        return await message.reply(
            "Готово: удалено из контент‑плана: "
            f"{result.removed_pending}; отключено флагов повтора: "
            f"{result.disabled_flags}; очищено автоудалений: "
            f"{result.cleared_autodelete}"
        )
    except Exception as exc:
        return await message.reply(f"Ошибка: {exc}")
