from __future__ import annotations

from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.ai_editor_runtime import (
    ai_result_actions_kb,
    ai_result_summary,
    apply_generated_text,
    request_prompt_key,
    run_editor_ai_request,
)
from app.bot.bot_instance import bot as tg_bot
from app.bot.editor_preview import delete_tracked_preview_messages
from app.bot.fsm.states import PostFSM
from app.core.callbacks import CB
from app.core.db import AsyncSessionLocal
from app.services.ai_generation import AIGenerationService


router = Router()
router.callback_query.filter(F.message.chat.type == "private")


@router.callback_query(F.data == CB.AI_RESET_HISTORY)
async def cb_ai_reset_history(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    request = dict(data.get("ai_last_request") or {})
    prompt_key = request_prompt_key(request)
    channel_id = int(data.get("channel_id") or 0)
    if not prompt_key or not channel_id:
        await callback.answer("Нет активного AI-диалога", show_alert=False)
        return

    async with AsyncSessionLocal() as session:
        service = AIGenerationService(session)
        deleted = await service.clear_history(
            user_id=callback.from_user.id,
            prompt_key=prompt_key,
            channel_id=channel_id,
        )
    await callback.answer(
        "Контекст очищен. «Ещё вариант» начнёт новый диалог."
        if deleted
        else "Контекст уже пуст.",
        show_alert=False,
    )


@router.callback_query(F.data == CB.AI_RETRY_LAST)
async def cb_ai_retry_last(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    request = dict(data.get("ai_last_request") or {})
    channel_id = int(data.get("channel_id") or 0)
    if not request or not channel_id:
        await callback.answer("Предыдущий AI-запрос не найден", show_alert=True)
        return

    await callback.answer("Генерирую новый вариант…")
    with suppress(TelegramBadRequest):
        await callback.message.delete()

    async with AsyncSessionLocal() as session:
        result = await run_editor_ai_request(
            tg_bot,
            session=session,
            request=request,
            channel_id=channel_id,
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            seed=callback.message.message_id,
        )

    if not result.get("success"):
        await tg_bot.send_message(
            chat_id=callback.message.chat.id,
            text=f"❌ Ошибка:\n{result.get('error') or 'Неизвестная ошибка'}",
            reply_markup=ai_result_actions_kb(),
        )
        await state.set_state(PostFSM.preview)
        return

    payload = apply_generated_text(
        dict(data.get("payload") or {}),
        result.get("text") or "",
    )
    await delete_tracked_preview_messages(
        tg_bot,
        chat_id=callback.message.chat.id,
        state_data=data,
    )
    await state.update_data(
        payload=payload,
        preview_msg_id=None,
        preview_text_id=None,
        preview_media_id=None,
        preview_album_ids=[],
    )

    # Imported lazily to avoid coupling the router module to the legacy main
    # router at import time while the editor preview is being modularized.
    from app.bot.routers.main import _send_preview_message

    preview = await _send_preview_message(
        callback.message,
        payload,
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
        is_draft=bool(data.get("is_draft", False)),
        edit_mode=bool(data.get("edit_mode", False)),
        has_buttons=bool(payload.get("buttons")),
        state=state,
    )
    await state.update_data(preview_msg_id=preview.message_id)
    await state.set_state(PostFSM.preview)
    await tg_bot.send_message(
        chat_id=callback.message.chat.id,
        text=ai_result_summary(result, label="Новый вариант готов"),
        reply_markup=ai_result_actions_kb(),
    )
