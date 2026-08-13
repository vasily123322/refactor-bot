from __future__ import annotations

from contextlib import suppress
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.core.db import AsyncSessionLocal
from app.services.canonical_pending_publication_cancel import (
    CanonicalPendingPublicationCancelService,
)


router = Router()
router.callback_query.filter(F.message.chat.type == "private")


@router.callback_query(F.data.startswith("cp_delete_post:"))
async def cb_cp_delete_pending_migration_control(
    callback: CallbackQuery,
    state: FSMContext,
):
    parts = str(callback.data or "").split(":")
    if len(parts) != 3:
        return await callback.answer("Ошибка данных", show_alert=True)
    try:
        post_task_id = int(parts[1])
        date_iso = datetime.fromisoformat(parts[2]).date().isoformat()
        user_id = int(callback.from_user.id)
    except (TypeError, ValueError, OverflowError):
        return await callback.answer("Ошибка данных", show_alert=True)

    async with AsyncSessionLocal() as session:
        result = await CanonicalPendingPublicationCancelService(
            session
        ).cancel_owned_pending(
            post_task_id=post_task_id,
            tg_user_id=user_id,
        )

    if result.outcome in {"cancelled", "legacy_deleted"}:
        try:
            data = await state.get_data()
            channel_id = int(data.get("cp_channel_id") or 0)
            if channel_id:
                from app.bot.routers.content_plan import _render_content_plan

                await _render_content_plan(
                    callback,
                    state,
                    channel_id,
                    datetime.fromisoformat(date_iso),
                )
        except Exception:
            pass
        with suppress(TelegramBadRequest):
            return await callback.answer("🗑 Удалено из очереди", show_alert=False)
        return None

    if result.outcome == "contention":
        return await callback.answer(
            "Публикация уже начала обработку; автоматическая отмена не выполнялась.",
            show_alert=True,
        )
    if result.outcome == "ineligible":
        return await callback.answer(
            "Эта запись уже не является pending. Legacy удаление истории отключено.",
            show_alert=True,
        )
    if result.outcome == "not_found":
        return await callback.answer("Публикация не найдена или нет доступа", show_alert=True)
    return await callback.answer(
        "Не удалось безопасно отменить публикацию из-за active/recovery authority.",
        show_alert=True,
    )
