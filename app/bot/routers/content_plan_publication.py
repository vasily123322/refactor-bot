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
from app.services.content_plan_publication_cancellation import (
    ContentPlanPublicationCancellationService,
)
from app.services.content_plan_publication_controls import (
    ContentPlanPublicationControlError,
    ContentPlanPublicationControlService,
    ContentPlanRepeatUnsupported,
    ContentPlanStaleControl,
    content_plan_schedule_state_token,
)
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


_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(value: int) -> str:
    number = int(value)
    if number <= 0:
        raise ValueError("publication id must be positive")
    chars: list[str] = []
    while number:
        number, remainder = divmod(number, 36)
        chars.append(_BASE36_DIGITS[remainder])
    return "".join(reversed(chars))


def _parse_base36(value: str) -> int:
    parsed = int(str(value), 36)
    if parsed <= 0:
        raise ValueError("publication id must be positive")
    return parsed


def _control_callback_data(
    prefix: str,
    *,
    publication_id: int,
    date_iso: str,
    schedule_token: str,
    value: int | None = None,
) -> str:
    canonical_date = date.fromisoformat(str(date_iso)).isoformat()
    token = str(schedule_token).strip().lower()
    if not token or any(ch not in _BASE36_DIGITS for ch in token):
        raise ValueError("invalid schedule state token")
    parts = [prefix, _base36(publication_id), canonical_date, token]
    if value is not None:
        parts.append(str(int(value)))
    callback_data = ":".join(parts)
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("Telegram callback_data exceeds 64 bytes")
    return callback_data


def _parse_stateful_control_callback(
    data: str | None,
    prefix: str,
) -> tuple[int, str, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != prefix:
        return None
    try:
        publication_id = _parse_base36(parts[1])
        date_iso = date.fromisoformat(str(parts[2])).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None
    token = str(parts[3]).strip().lower()
    if not token or any(ch not in _BASE36_DIGITS for ch in token):
        return None
    return publication_id, date_iso, token


def _view_schedule_token(view) -> str:
    if view.schedule_entry_id is None:
        raise ValueError("canonical publication is missing schedule identity")
    return content_plan_schedule_state_token(
        schedule_entry_id=int(view.schedule_entry_id),
        scheduled_at=view.scheduled_at,
        repeat_rule=view.repeat_rule,
    )


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
    elif view.status == "queued":
        schedule_token = _view_schedule_token(view)
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
                    text="Изменить дату",
                    callback_data=_control_callback_data(
                        "cp_resched_pub",
                        publication_id=view.publication_id,
                        date_iso=date_iso,
                        schedule_token=schedule_token,
                    ),
                ),
                InlineKeyboardButton(
                    text=(
                        "🔁 Повтор включён"
                        if view.repeat_rule.get("enabled") is True
                        else "🔁 Повтор"
                    ),
                    callback_data=_control_callback_data(
                        "cp_repeat_pub",
                        publication_id=view.publication_id,
                        date_iso=date_iso,
                        schedule_token=schedule_token,
                    ),
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Удалить",
                    callback_data=_control_callback_data(
                        "cp_delete_pub",
                        publication_id=view.publication_id,
                        date_iso=date_iso,
                        schedule_token=schedule_token,
                    ),
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


@router.callback_query(F.data.startswith("cp_delete_pub:"))
async def cb_cp_delete_publication(callback: CallbackQuery, state: FSMContext):
    parsed = _parse_stateful_control_callback(callback.data, "cp_delete_pub")
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso, schedule_token = parsed

    async with AsyncSessionLocal() as session:
        view = await load_owned_publication_editor_view(
            session,
            publication_id=publication_id,
            tg_user_id=int(callback.from_user.id),
        )
    if view is None:
        return await callback.answer("Публикация не найдена или нет доступа", show_alert=True)
    if view.status != "queued":
        return await callback.answer(
            "Публикацию сейчас нельзя безопасно отменить",
            show_alert=True,
        )

    try:
        result = await ContentPlanPublicationCancellationService(
            AsyncSessionLocal
        ).delete(
            publication_id,
            expected_schedule_token=schedule_token,
        )
    except Exception:
        return await callback.answer("Не удалось удалить", show_alert=True)
    if result.outcome == "cannot_cancel":
        if result.reason == "stale_schedule_state":
            return await callback.answer(
                "Эта кнопка устарела — откройте публикацию заново",
                show_alert=True,
            )
        return await callback.answer(
            "Публикацию сейчас нельзя безопасно отменить",
            show_alert=True,
        )

    await state.update_data(cp_channel_id=view.channel_id, cp_center=date_iso)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Назад",
                    callback_data=f"cp_open_cal:{view.channel_id}:{date_iso}",
                )
            ]
        ]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("Публикация отменена 🚫", reply_markup=kb)
    with suppress(TelegramBadRequest):
        await callback.answer("🗑 Удалено", show_alert=False)


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
    if view.status not in {"queued", "published"}:
        return await callback.answer(
            "Публикация сейчас недоступна для редактирования", show_alert=True
        )
    if view.status == "published" and view.primary_message_id is None:
        return await callback.answer(
            "Публикация ещё не готова для редактирования", show_alert=True
        )

    try:
        payload = view.editor_payload()
    except (LegacyPayloadError, ValueError, TypeError):
        return await callback.answer(
            "Этот формат редактируется через Studio", show_alert=True
        )

    queued = view.status == "queued"
    await state.update_data(
        channel_id=view.channel_id,
        edit_chat_id=None if queued else view.tg_chat_id,
        edit_msg_id=None if queued else view.primary_message_id,
        result_ids=None if queued else list(view.telegram_message_ids),
        payload=payload,
        repeat_on=bool(view.repeat_rule.get("enabled", False)),
        repeat_seconds=int(view.repeat_rule.get("seconds") or 0),
        notify_on=True,
        autosign_on=False,
        pin_on=False,
        comments_on=True,
        is_draft=False,
        canonical_edit_context={
            "mode": "queued" if queued else "published",
            "publication_id": view.publication_id,
            "expected_revision": view.content_revision,
        },
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


def _parse_control_callback(
    data: str | None,
    prefix: str,
) -> tuple[int, str, str, int] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 5 or parts[0] != prefix:
        return None
    parsed = _parse_stateful_control_callback(
        ":".join(parts[:4]),
        prefix,
    )
    if parsed is None:
        return None
    publication_id, date_iso, token = parsed
    try:
        value = int(parts[4])
    except (TypeError, ValueError, OverflowError):
        return None
    return publication_id, date_iso, token, value


async def _reload_publication_card(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    publication_id: int,
    date_iso: str,
) -> None:
    callback2 = callback.model_copy(
        update={"data": f"cp_open_pub:{int(publication_id)}:{date_iso}"}
    )
    await cb_cp_open_publication(callback2, state)


@router.callback_query(F.data.startswith("cp_resched_pub:"))
async def cb_cp_reschedule_publication(callback: CallbackQuery, state: FSMContext):
    parsed = _parse_stateful_control_callback(callback.data, "cp_resched_pub")
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso, schedule_token = parsed
    async with AsyncSessionLocal() as session:
        view = await load_owned_publication_editor_view(
            session,
            publication_id=publication_id,
            tg_user_id=int(callback.from_user.id),
        )
    if view is None or view.status != "queued":
        return await callback.answer("Публикация недоступна", show_alert=True)
    if _view_schedule_token(view) != schedule_token:
        return await callback.answer(
            "Эта кнопка устарела — откройте публикацию заново",
            show_alert=True,
        )

    rows = [
        [
            InlineKeyboardButton(
                text="+1 ч",
                callback_data=_control_callback_data(
                    "cp_resched_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=3600,
                ),
            ),
            InlineKeyboardButton(
                text="+6 ч",
                callback_data=_control_callback_data(
                    "cp_resched_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=21600,
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                text="+1 день",
                callback_data=_control_callback_data(
                    "cp_resched_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=86400,
                ),
            ),
            InlineKeyboardButton(
                text="+7 дней",
                callback_data=_control_callback_data(
                    "cp_resched_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=604800,
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"cp_open_pub:{publication_id}:{date_iso}",
            )
        ],
    ]
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Перенести публикацию относительно текущего времени:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("cp_resched_set_pub:"))
async def cb_cp_reschedule_set_publication(callback: CallbackQuery, state: FSMContext):
    parsed = _parse_control_callback(callback.data, "cp_resched_set_pub")
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso, schedule_token, delta_seconds = parsed
    if delta_seconds <= 0:
        return await callback.answer("Ошибка интервала", show_alert=True)

    try:
        result = await ContentPlanPublicationControlService(
            AsyncSessionLocal
        ).reschedule(
            publication_id=publication_id,
            tg_user_id=int(callback.from_user.id),
            expected_schedule_token=schedule_token,
            delta_seconds=delta_seconds,
        )
    except ContentPlanStaleControl:
        return await callback.answer(
            "Эта кнопка устарела — откройте публикацию заново",
            show_alert=True,
        )
    except ContentPlanPublicationControlError:
        return await callback.answer("Не удалось перенести публикацию", show_alert=True)

    new_date = result.scheduled_at
    if result.timezone:
        with suppress(Exception):
            new_date = new_date.astimezone(ZoneInfo(result.timezone))
    await _reload_publication_card(
        callback,
        state,
        publication_id=publication_id,
        date_iso=new_date.date().isoformat(),
    )


@router.callback_query(F.data.startswith("cp_repeat_pub:"))
async def cb_cp_repeat_publication(callback: CallbackQuery, state: FSMContext):
    parsed = _parse_stateful_control_callback(callback.data, "cp_repeat_pub")
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso, schedule_token = parsed
    async with AsyncSessionLocal() as session:
        view = await load_owned_publication_editor_view(
            session,
            publication_id=publication_id,
            tg_user_id=int(callback.from_user.id),
        )
    if view is None or view.status != "queued":
        return await callback.answer("Публикация недоступна", show_alert=True)
    if _view_schedule_token(view) != schedule_token:
        return await callback.answer(
            "Эта кнопка устарела — откройте публикацию заново",
            show_alert=True,
        )

    rows = [
        [
            InlineKeyboardButton(
                text="Выкл",
                callback_data=_control_callback_data(
                    "cp_repeat_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=0,
                ),
            ),
            InlineKeyboardButton(
                text="1 ч",
                callback_data=_control_callback_data(
                    "cp_repeat_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=3600,
                ),
            ),
            InlineKeyboardButton(
                text="6 ч",
                callback_data=_control_callback_data(
                    "cp_repeat_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=21600,
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                text="12 ч",
                callback_data=_control_callback_data(
                    "cp_repeat_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=43200,
                ),
            ),
            InlineKeyboardButton(
                text="1 день",
                callback_data=_control_callback_data(
                    "cp_repeat_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=86400,
                ),
            ),
            InlineKeyboardButton(
                text="3 дня",
                callback_data=_control_callback_data(
                    "cp_repeat_set_pub",
                    publication_id=publication_id,
                    date_iso=date_iso,
                    schedule_token=schedule_token,
                    value=259200,
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"cp_open_pub:{publication_id}:{date_iso}",
            )
        ],
    ]
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Автоповтор canonical-публикации:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("cp_repeat_set_pub:"))
async def cb_cp_repeat_set_publication(callback: CallbackQuery, state: FSMContext):
    parsed = _parse_control_callback(callback.data, "cp_repeat_set_pub")
    if parsed is None:
        return await callback.answer("Ошибка данных", show_alert=True)
    publication_id, date_iso, schedule_token, repeat_seconds = parsed
    try:
        await ContentPlanPublicationControlService(AsyncSessionLocal).set_repeat(
            publication_id=publication_id,
            tg_user_id=int(callback.from_user.id),
            repeat_seconds=repeat_seconds or None,
            expected_schedule_token=schedule_token,
        )
    except ContentPlanRepeatUnsupported:
        return await callback.answer(
            "Автоповтор недоступен для time+views удаления",
            show_alert=True,
        )
    except ContentPlanStaleControl:
        return await callback.answer(
            "Эта кнопка устарела — откройте публикацию заново",
            show_alert=True,
        )
    except ContentPlanPublicationControlError:
        return await callback.answer("Не удалось изменить автоповтор", show_alert=True)

    await _reload_publication_card(
        callback,
        state,
        publication_id=publication_id,
        date_iso=date_iso,
    )
