from __future__ import annotations

import html
from contextlib import suppress
from datetime import date, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.fsm.states import PostFSM
from app.core.db import AsyncSessionLocal
from app.services.content import LegacyPayloadError
from app.services.publication_editor import (
    load_owned_publication_editor_view,
    publication_edit_callback,
)


router = Router()
router.callback_query.filter(F.message.chat.type == "private")


def _parse_callback(data: str | None, prefix: str) -> tuple[int, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        publication_id = int(parts[1])
        date_iso = date.fromisoformat(str(parts[2])).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None
    if publication_id <= 0:
        return None
    return publication_id, date_iso


def _scheduled_label(view) -> str:
    value = view.scheduled_at
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if view.timezone:
        with suppress(Exception):
            value = value.astimezone(ZoneInfo(view.timezone))
    return value.strftime("%d.%m.%Y %H:%M")


def _status_label(status: str) -> str:
    return {
        "published": "Опубликован ✅",
        "queued": "Отложен ⏳",
        "sending": "Публикуется ⏳",
        "failed": "Ошибка ❌",
        "skipped": "Пропущен ⏭",
        "cancelled": "Отменён 🚫",
    }.get(str(status or "").lower(), html.escape(str(status or "—")))


@router.callback_query(F.data.startswith("cp_open_pub:"))
async def cb_cp_open_publication(callback: CallbackQuery, state: FSMContext):
    parsed = _parse_callback(callback.data, "cp_open_pub")
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso = parsed

    async with AsyncSessionLocal() as session:
        view = await load_owned_publication_editor_view(
            session,
            publication_id=publication_id,
            tg_user_id=int(callback.from_user.id),
        )
    if view is None:
        return await callback.answer("Публикация не найдена или нет доступа", show_alert=True)

    payload: dict = {}
    with suppress(LegacyPayloadError, ValueError, TypeError):
        payload = view.editor_payload()

    result_line = (
        f'<a href="{html.escape(view.result_link, quote=True)}">Открыть публикацию</a>'
        if view.result_link
        else "Ссылка: нет"
    )
    text = (
        f"Статус: {_status_label(view.status)}\n"
        f"{result_line}\n"
        f"Канал: {html.escape(view.channel_title)}\n"
        f"Дата: {_scheduled_label(view)}"
    )

    rows: list[list[InlineKeyboardButton]] = []
    if view.status == "published" and view.primary_message_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Изменить",
                    callback_data=publication_edit_callback(view.publication_id, date_iso),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"cp_open_cal:{view.channel_id}:{date_iso}",
            )
        ]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await state.update_data(
        cp_channel_id=view.channel_id,
        cp_center=date_iso,
        canonical_return_to_notice={
            "publication_id": view.publication_id,
            "date": date_iso,
        },
        payload=payload,
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        with suppress(TelegramBadRequest):
            await callback.message.answer(
                text,
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    await callback.answer()


@router.callback_query(F.data.startswith("cp_edit_pub:"))
async def cb_cp_edit_publication(callback: CallbackQuery, state: FSMContext):
    parsed = _parse_callback(callback.data, "cp_edit_pub")
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso = parsed

    async with AsyncSessionLocal() as session:
        view = await load_owned_publication_editor_view(
            session,
            publication_id=publication_id,
            tg_user_id=int(callback.from_user.id),
        )
    if view is None:
        return await callback.answer("Публикация не найдена или нет доступа", show_alert=True)
    if view.status != "published" or view.primary_message_id is None:
        return await callback.answer(
            "Публикация ещё не готова для редактирования", show_alert=True
        )

    try:
        payload = view.editor_payload()
    except (LegacyPayloadError, ValueError, TypeError):
        return await callback.answer(
            "Этот формат редактируется через Studio", show_alert=True
        )

    await state.update_data(
        channel_id=view.channel_id,
        edit_chat_id=view.tg_chat_id,
        edit_msg_id=view.primary_message_id,
        result_ids=list(view.telegram_message_ids),
        payload=payload,
        repeat_on=bool(view.repeat_rule.get("enabled", False)),
        repeat_seconds=int(view.repeat_rule.get("seconds") or 0),
        notify_on=True,
        autosign_on=False,
        pin_on=False,
        comments_on=True,
        is_draft=False,
        canonical_return_to_notice={
            "publication_id": view.publication_id,
            "date": date_iso,
        },
        prev_editor_restore={
            "type": "cp_publication_card",
            "publication_id": view.publication_id,
            "date": date_iso,
        },
    )

    from app.bot.routers.main import _send_preview_message

    preview = await _send_preview_message(
        callback.message,
        payload,
        notify_on=True,
        autosign_on=False,
        pin_on=False,
        comments_on=True,
        is_draft=False,
        edit_mode=True,
        state=state,
    )
    await state.update_data(preview_msg_id=preview.message_id)
    await state.set_state(PostFSM.preview)
    with suppress(TelegramBadRequest):
        await callback.answer()
