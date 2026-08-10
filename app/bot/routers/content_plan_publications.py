from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.bot_instance import bot as tg_bot
from app.bot.fsm.states import PostFSM
from app.bot.routers.shared import escape_markdown_label as _escape_markdown_label
from app.core.callbacks import CB
from app.core.db import AsyncSessionLocal
from app.services.content_plan_publications import (
    OwnedPublicationContext,
    load_owned_publication_context,
)


router = Router()
router.callback_query.filter(F.message.chat.type == "private")

_OPEN_PREFIX = "cp_open_publication:"
_EDIT_PREFIX = "cp_edit_publication:"


def _parse_callback(data: str | None, prefix: str) -> tuple[int, str] | None:
    if not data or not data.startswith(prefix):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        publication_id = int(parts[1])
        datetime.fromisoformat(parts[2])
    except (TypeError, ValueError, OverflowError):
        return None
    if publication_id <= 0:
        return None
    return publication_id, parts[2]


def _humanize_seconds(raw: object) -> str | None:
    try:
        seconds = int(raw or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if seconds <= 0:
        return None
    minutes = max(1, seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes} мин")
    return " ".join(parts) or "<1ч"


def _autodelete_line(ctx: OwnedPublicationContext) -> str:
    runtime = ctx.publication_meta.get("autodelete_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    options = ctx.publication_meta.get("runtime_options") or ctx.schedule_meta.get(
        "runtime_options"
    )
    options = options if isinstance(options, dict) else {}

    try:
        views = int(options.get("autodelete_views") or 0)
    except (TypeError, ValueError, OverflowError):
        views = 0
    if views > 0:
        return f"👁 {views}"

    duration = _humanize_seconds(
        runtime.get("effective_seconds") or options.get("autodelete_seconds")
    )
    return f"🗑️ {duration}" if duration else "Таймер удаления: нет"


def _local_time(ctx: OwnedPublicationContext) -> str:
    if ctx.scheduled_at is None:
        return ""
    value = ctx.scheduled_at
    try:
        if ctx.timezone_name:
            value = value.astimezone(ZoneInfo(ctx.timezone_name))
    except Exception:
        pass
    return value.strftime("%d.%m.%Y %H:%M")


async def _result_link(ctx: OwnedPublicationContext) -> str | None:
    if ctx.result_link:
        return ctx.result_link
    if not ctx.telegram_message_ids:
        return None
    message_id = ctx.telegram_message_ids[-1]
    try:
        chat = await tg_bot.get_chat(ctx.tg_chat_id)
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{message_id}"
        raw = str(ctx.tg_chat_id)
        if raw.startswith("-100"):
            return f"https://t.me/c/{raw[4:]}/{message_id}"
    except Exception:
        return None
    return None


async def _load_owned(callback: CallbackQuery, publication_id: int) -> OwnedPublicationContext | None:
    user_id = int(getattr(callback.from_user, "id", 0) or 0)
    if user_id <= 0:
        return None
    async with AsyncSessionLocal() as session:
        return await load_owned_publication_context(
            session,
            publication_id=publication_id,
            tg_user_id=user_id,
        )


@router.callback_query(F.data.startswith(_OPEN_PREFIX))
async def cb_cp_open_publication(callback: CallbackQuery, state: FSMContext) -> None:
    parsed = _parse_callback(callback.data, _OPEN_PREFIX)
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso = parsed
    ctx = await _load_owned(callback, publication_id)
    if ctx is None:
        return await callback.answer("Публикация не найдена или нет доступа", show_alert=True)
    if ctx.status != "published":
        return await callback.answer("Публикация ещё не завершена", show_alert=True)

    link = await _result_link(ctx)
    channel_title = _escape_markdown_label(ctx.channel_title or str(ctx.tg_chat_id))
    text = (
        "Статус: Опубликован ✅\n"
        f"Ссылка: {link or 'нет'}\n"
        f"Канал: {channel_title}\n"
        f"Дата: {_local_time(ctx)}\n"
        f"{_autodelete_line(ctx)}"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="Изменить",
                callback_data=f"{_EDIT_PREFIX}{ctx.publication_id}:{date_iso}",
            )
        ]
    ]
    # Preserve existing legacy actions while the compatibility row still exists.
    # Once published retention is enabled later, the canonical editor/back path stays
    # usable and transport-only actions simply disappear instead of dereferencing a
    # deleted PostTask.
    if ctx.legacy_post_task_id is not None:
        rows[0].insert(0, InlineKeyboardButton(text="Дублировать", callback_data=CB.EDIT_DUP))
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="☑️ Это рекламный пост", callback_data=CB.EDIT_AD_TOGGLE
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=_autodelete_line(ctx), callback_data=CB.EDIT_AUTODEL
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=f"cp_delete_post:{ctx.legacy_post_task_id}:{date_iso}",
                    )
                ],
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"cp_open_cal:{ctx.channel_id}:{date_iso}",
            )
        ]
    )

    notice: dict[str, object] = {
        "publication_id": ctx.publication_id,
        "date": date_iso,
    }
    if ctx.legacy_post_task_id is not None:
        notice["post_id"] = ctx.legacy_post_task_id
    await state.update_data(return_to_notice=notice)

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest:
        with suppress(TelegramBadRequest):
            await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith(_EDIT_PREFIX))
async def cb_cp_edit_publication(callback: CallbackQuery, state: FSMContext) -> None:
    parsed = _parse_callback(callback.data, _EDIT_PREFIX)
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso = parsed
    ctx = await _load_owned(callback, publication_id)
    if ctx is None:
        return await callback.answer("Публикация не найдена или нет доступа", show_alert=True)
    if ctx.status != "published":
        return await callback.answer("Публикация ещё не опубликована", show_alert=True)
    if not ctx.telegram_message_ids:
        return await callback.answer(
            "Не удалось определить сообщение для редактирования", show_alert=True
        )

    message_id = int(ctx.telegram_message_ids[-1])
    restore: dict[str, object] = {
        "type": "cp_publication_card",
        "publication_id": ctx.publication_id,
        "date": date_iso,
    }
    if ctx.legacy_post_task_id is not None:
        # Existing editor-back behavior understands cp_card today. Keep it while
        # transport exists; the canonical publication identity remains alongside it.
        restore.update(
            {
                "type": "cp_card",
                "post_id": ctx.legacy_post_task_id,
                "publication_id": ctx.publication_id,
            }
        )

    await state.update_data(
        channel_id=ctx.channel_id,
        edit_chat_id=ctx.tg_chat_id,
        edit_msg_id=message_id,
        payload=dict(ctx.editor_payload),
        notify_on=True,
        autosign_on=False,
        pin_on=False,
        comments_on=True,
        is_draft=False,
        prev_editor_restore=restore,
        return_to_notice={
            "publication_id": ctx.publication_id,
            "post_id": ctx.legacy_post_task_id,
            "date": date_iso,
        },
    )

    try:
        previous = await state.get_data()
        preview_id = previous.get("preview_msg_id")
        if preview_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id,
                    message_id=int(preview_id),
                )
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
            )
    except Exception:
        pass

    from app.bot.routers.main import _send_preview_message

    preview = await _send_preview_message(
        callback.message,
        dict(ctx.editor_payload),
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
