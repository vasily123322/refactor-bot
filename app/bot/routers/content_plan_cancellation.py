from __future__ import annotations

from contextlib import suppress
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.routers.content_plan import (
    _render_content_plan,
    router as legacy_content_plan_router,
)
from app.core.db import AsyncSessionLocal
from app.services.content_plan_cancellation import ContentPlanCancellationService


router = Router()


@router.callback_query(F.data.startswith("cp_delete_post:"))
async def cb_cp_delete_post_canonical(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Canonical-aware authority for the content-plan Delete button.

    This handler is registered on the wrapper router before the legacy content-plan
    router. Matching Delete callbacks therefore cannot reach the legacy raw
    ``session.delete(PostTask)`` handler underneath it.
    """

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, post_id_str, date_iso = parts
    try:
        post_id = int(post_id_str)
    except (TypeError, ValueError):
        await callback.answer("Ошибка данных", show_alert=True)
        return

    try:
        result = await ContentPlanCancellationService(AsyncSessionLocal).delete(post_id)
    except Exception:
        await callback.answer("Не удалось удалить", show_alert=True)
        return

    if result.outcome == "cannot_cancel":
        await callback.answer(
            "Публикацию сейчас нельзя безопасно отменить",
            show_alert=True,
        )
        return

    # Preserve the existing navigation contract after a successful/terminal Delete.
    try:
        cid = int((await state.get_data()).get("cp_channel_id") or 0)
        if not cid:
            await callback.answer("Удалено", show_alert=False)
            return
        center = datetime.fromisoformat(date_iso)
        await _render_content_plan(callback, state, cid, center)
        with suppress(TelegramBadRequest):
            await callback.answer("🗑 Удалено", show_alert=False)
    except Exception:
        await callback.answer("🗑 Удалено", show_alert=False)


# Aiogram resolves the wrapper's own observers before descending into subrouters.
# The legacy router remains intact for all non-Delete content-plan callbacks, while
# its historical raw Delete handler is shadowed by the authority above.
router.include_router(legacy_content_plan_router)
