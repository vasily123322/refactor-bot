from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from contextlib import suppress

from app.core.callbacks import CB
from app.bot.fsm.states import PostFSM
from app.bot.bot_instance import bot as tg_bot
from app.core.db import AsyncSessionLocal
from app.services.manual_publish import ManualPublishService
from app.services.posting import PostingService
from app.bot.routers.shared import safe_answer as _safe_answer
from app.bot.keyboards.posting import settings_menu_kb

# Helpers reused from main router to avoid duplication
from app.bot.routers.utils.post_payload import (
    maybe_append_autosign as _maybe_append_autosign,
    clip_text_len as _clip_text_len,
    edit_text_with_fallback as _edit_text_with_fallback,
    edit_media_with_fallback as _edit_media_with_fallback,
    _iso_to_datetime,
    _apply_repeat_flags_from_state_to_payload,
    _set_autodelete_effective,
    _gate_short_autodelete_in_payload,
    _apply_autosign_if_enabled,
    _schedule_next_repeat_if_pro,
    _build_scheduled_confirmation,
    _render_forward_menu,
    _add_author_meta_from_user,
)

from app.repositories.clients import ClientsRepo
from app.repositories.channels import ChannelsRepo


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


async def _ensure_ui_settings(
    state: FSMContext, user, *, data: dict | None = None
) -> dict[str, bool]:
    if data is None:
        data = await state.get_data()
    ui_settings = dict(data.get("ui_settings") or {})
    ui_client_id = data.get("ui_client_id")
    if ui_settings and ui_client_id is not None:
        return ui_settings
    if user is None:
        return ui_settings
    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        client = await clients.create_or_get(user.id, user.username, user.full_name)
        toggles = await clients.get_ui_settings(client.id)
    await state.update_data(ui_settings=toggles, ui_client_id=int(client.id))
    return dict(toggles)


async def _render_publish_settings_view(
    callback: CallbackQuery, state: FSMContext, data: dict
) -> None:
    payload = dict(data.get("payload") or {})
    cur_seconds = int(payload.get("autodelete_seconds") or 0)
    cur_views = int(payload.get("autodelete_views") or 0)
    kb = settings_menu_kb(
        timer_set=bool(cur_seconds),
        repeat_on=bool(data.get("repeat_on", False)),
        time_seconds=cur_seconds,
        views_value=cur_views,
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
    )
    text = "⚙️ <b>Настройки публикации</b>\n\nУстановите копирование в каналы, таймер удаления, закреп и другие параметры поста."
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )
    with suppress(Exception):
        await state.update_data(
            settings_msg_id=int(callback.message.message_id), ui_submenu="settings"
        )


@router.callback_query(F.data == CB.POST_SCHEDULE)
async def cb_post_schedule(callback: CallbackQuery, state: FSMContext):
    # Открыть календарь контент‑плана «По расписанию» для текущего канала
    data = await state.get_data()
    chan_id = int(data.get("channel_id") or 0)
    if not chan_id:
        return await callback.answer("Сначала выберите канал", show_alert=True)
    from datetime import datetime as _dt

    date_iso = _dt.now().date().isoformat()
    try:
        from app.bot.routers.content_plan import cb_cp_open_calendar as _open_cal

        # Сымитируем клик по cp_open_cal:cid:YYYY-MM-DD
        cb2 = callback.model_copy(update={"data": f"cp_open_cal:{chan_id}:{date_iso}"})  # type: ignore
        await _open_cal(cb2)
    except Exception:
        pass
    return await _safe_answer(callback)


@router.callback_query(F.data == CB.POST_SEND)
async def cb_post_send(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    # Если редактируем существующий пост
    edit_chat_id = data.get("edit_chat_id")
    edit_msg_id = data.get("edit_msg_id")
    if isinstance(data.get("canonical_edit_context"), dict):
        from app.bot.routers.utils.canonical_publication_edit import (
            handle_canonical_publication_edit,
        )

        await handle_canonical_publication_edit(
            callback,
            state,
            data=data,
            payload=dict(data.get("payload") or {}),
        )
        return
    if edit_chat_id and edit_msg_id and not bool(data.get("is_draft", False)):
        payload: dict = dict(data.get("payload") or {})
        prev_id = data.get("preview_msg_id")
        try:
            if payload.get("type") == "text":
                text0 = await _maybe_append_autosign(payload.get("text", ""), data)
                text_safe = _clip_text_len(text0, 4096)
                new_id = await _edit_text_with_fallback(
                    edit_chat_id,
                    edit_msg_id,
                    list(data.get("result_ids") or []),
                    text_safe,
                )
                await state.update_data(edit_msg_id=int(new_id))
            elif payload.get("type") == "photo":
                from aiogram.types import InputMediaPhoto

                cap0 = await _maybe_append_autosign(payload.get("caption") or "", data)
                cap_safe = _clip_text_len(cap0, 1024)
                media = InputMediaPhoto(
                    media=payload.get("file_id"),
                    caption=cap_safe,
                    parse_mode="Markdown",
                    has_spoiler=bool(payload.get("media_spoiler", False)),
                    show_caption_above_media=(
                        str(payload.get("media_pos")) == "bottom"
                    ),
                )
                new_id = await _edit_media_with_fallback(
                    edit_chat_id, edit_msg_id, list(data.get("result_ids") or []), media
                )
                if new_id:
                    await state.update_data(edit_msg_id=int(new_id))
            elif payload.get("type") == "video":
                from aiogram.types import InputMediaVideo

                cap0 = await _maybe_append_autosign(payload.get("caption") or "", data)
                cap_safe = _clip_text_len(cap0, 1024)
                media = InputMediaVideo(
                    media=payload.get("file_id"),
                    caption=cap_safe,
                    parse_mode="Markdown",
                    has_spoiler=bool(payload.get("media_spoiler", False)),
                    show_caption_above_media=(
                        str(payload.get("media_pos")) == "bottom"
                    ),
                )
                new_id = await _edit_media_with_fallback(
                    edit_chat_id, edit_msg_id, list(data.get("result_ids") or []), media
                )
                if new_id:
                    await state.update_data(edit_msg_id=int(new_id))
            elif payload.get("type") == "animation":
                from aiogram.types import InputMediaAnimation

                media = InputMediaAnimation(
                    media=payload.get("file_id"),
                    caption=payload.get("caption"),
                    parse_mode="Markdown",
                )
                await tg_bot.edit_message_media(
                    chat_id=edit_chat_id, message_id=edit_msg_id, media=media
                )
            elif payload.get("type") == "audio":
                from aiogram.types import InputMediaAudio

                media = InputMediaAudio(
                    media=payload.get("file_id"),
                    caption=payload.get("caption"),
                    parse_mode="Markdown",
                )
                await tg_bot.edit_message_media(
                    chat_id=edit_chat_id, message_id=edit_msg_id, media=media
                )
            else:
                return await callback.answer(
                    "Нельзя отредактировать этот тип. Пересоздайте пост.",
                    show_alert=True,
                )
            # Успешно
            if prev_id:
                with suppress(TelegramBadRequest):
                    await tg_bot.delete_message(
                        chat_id=callback.message.chat.id, message_id=prev_id
                    )
            if bool(data.get("pin_on", False)):
                with suppress(Exception):
                    await tg_bot.pin_chat_message(
                        chat_id=edit_chat_id, message_id=edit_msg_id
                    )
            ret = (await state.get_data()).get("return_to_notice")
            if isinstance(ret, dict) and ret.get("post_id") and ret.get("date"):
                try:
                    from app.bot.routers.content_plan import cb_cp_open_post

                    await cb_cp_open_post(callback, state)
                    with suppress(TelegramBadRequest):
                        await callback.answer()
                except Exception:
                    pass
                return
            with suppress(TelegramBadRequest):
                await callback.answer("Готово")
            await state.clear()
            return
        except TelegramBadRequest as e:
            return await callback.answer(
                f"Не удалось отредактировать: {e}", show_alert=True
            )

    # Немедленная публикация выполняется ровно один раз. Если включён Pro repeat,
    # успешная provider отправка мостится максимум в один будущий canonical root.
    data2 = await state.get_data()
    chan_id2 = int(data2.get("channel_id", 0) or 0)
    payload2: dict = dict(data2.get("payload") or {})
    if not chan_id2 or not payload2:
        return await callback.answer("Нет данных поста", show_alert=True)

    _apply_repeat_flags_from_state_to_payload(data2, payload2)
    _set_autodelete_effective(payload2)
    await _gate_short_autodelete_in_payload(chan_id2, payload2)
    payload2 = await _apply_autosign_if_enabled(chan_id2, data2, payload2)
    payload2 = _add_author_meta_from_user(callback.from_user, payload2)

    from app.domain.models import Client as _Client
    from app.repositories.channels import ChannelsRepo as _ChRepo

    is_pro2 = False
    async with AsyncSessionLocal() as session:
        channel = await _ChRepo(session).get_by_id(chan_id2)
        if channel:
            owner = await session.get(_Client, int(getattr(channel, "owner_id", 0)))
            is_pro2 = bool(getattr(owner, "is_premium", False)) if owner else False

    result = await ManualPublishService(tg_bot, AsyncSessionLocal).publish(
        channel_id=chan_id2,
        payload=payload2,
        forward_to=list(data2.get("forward_to") or []),
        notify_on=bool(data2.get("notify_on", True)),
        repeat_on=bool(data2.get("repeat_on", False)),
        repeat_seconds=int(data2.get("repeat_seconds") or 0),
        repeat_allowed=is_pro2,
    )
    if not result.sent:
        return await callback.answer("Не удалось опубликовать", show_alert=True)

    await state.clear()
    return await callback.answer("Опубликовано")


@router.callback_query(F.data == CB.POST_SETTINGS_PUBLISH)
async def cb_post_settings_publish(callback: CallbackQuery, state: FSMContext):
    # Кнопка «🔥 Опубликовать»: если есть defer_at — создаём задачу в планировщике, иначе отправляем сейчас
    data = await state.get_data()
    ui_settings = await _ensure_ui_settings(state, callback.from_user, data=data)
    defer_iso = data.get("defer_at")
    action = "schedule" if defer_iso else "send"
    confirm_required = bool(ui_settings.get("confirm_publish", False))
    override = data.get("_confirm_publish_override")
    if confirm_required and override != action:
        await state.update_data(awaiting_publish_confirm=action)
        if defer_iso:
            when = _iso_to_datetime(defer_iso)
            when_label = when.strftime("%d.%m.%Y %H:%M")
            text = (
                "⚠️ <b>Подтверждение публикации</b>\n\n"
                f"Пост будет запланирован на <b>{when_label}</b>.\n\n"
                "Подтвердите действие."
            )
        else:
            text = (
                "⚠️ <b>Подтверждение публикации</b>\n\n"
                "Пост будет отправлен сразу в выбранный канал.\n\n"
                "Подтвердите действие."
            )
        kb_confirm = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить", callback_data=CB.POST_CONFIRM_PUBLISH_OK
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Отмена", callback_data=CB.POST_CONFIRM_PUBLISH_CANCEL
                    )
                ],
            ]
        )
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                text, reply_markup=kb_confirm, parse_mode="HTML"
            )
        return await callback.answer("Подтвердите публикацию")
    if override == action:
        await state.update_data(_confirm_publish_override=None)
    await state.update_data(awaiting_publish_confirm=None)
    if defer_iso:
        chan_id = int(data.get("channel_id", 0) or 0)
        payload: dict = dict(data.get("payload") or {})
        if not chan_id or not payload:
            return await callback.answer("Нет данных поста", show_alert=True)
        when = _iso_to_datetime(defer_iso)
        _apply_repeat_flags_from_state_to_payload(data, payload)
        _set_autodelete_effective(payload)
        await _gate_short_autodelete_in_payload(chan_id, payload)
        payload = await _apply_autosign_if_enabled(chan_id, data, payload)
        async with AsyncSessionLocal() as session:
            service = PostingService(tg_bot, session)
        payload = _add_author_meta_from_user(callback.from_user, payload)  # type: ignore[name-defined]
        await service.schedule(chan_id, payload, when)
        await _schedule_next_repeat_if_pro(service, chan_id, payload, data, when)
        text, kb = await _build_scheduled_confirmation(chan_id, defer_iso)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                text,
                reply_markup=kb,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        await state.clear()
        return await callback.answer("Запланировано")
    # Иначе — немедленная отправка с учётом повтора
    return await cb_post_send(callback, state)


@router.callback_query(F.data == CB.POST_CONFIRM_PUBLISH_CANCEL)
async def cb_post_confirm_publish_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(
        awaiting_publish_confirm=None, _confirm_publish_override=None
    )
    await _render_publish_settings_view(callback, state, data)
    await callback.answer("Отменено")


@router.callback_query(F.data == CB.POST_CONFIRM_PUBLISH_OK)
async def cb_post_confirm_publish_ok(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    action = data.get("awaiting_publish_confirm")
    if action not in {"send", "schedule"}:
        return await callback.answer("Нечего подтверждать", show_alert=True)
    await state.update_data(awaiting_publish_confirm=None)
    if action == "schedule":
        await state.update_data(_confirm_publish_override="schedule")
        return await cb_post_settings_publish(callback, state)
    await state.update_data(_confirm_publish_override=None)
    return await cb_post_send(callback, state)


@router.callback_query(F.data == CB.POST_SETTINGS_FORWARD)
async def cb_post_settings_forward(callback: CallbackQuery, state: FSMContext):
    # Меню выбора каналов для пересылки
    await _render_forward_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == CB.POST_FWD_ALL)
async def cb_post_fwd_all(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    main_cid = int(data.get("channel_id") or 0)
    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        channels = ChannelsRepo(session)
        client = await clients.create_or_get(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.full_name,
        )
        items = await channels.list_by_owner(client.id)
    all_ids = [int(ch.id) for ch in (items or []) if int(ch.id) != main_cid]
    await state.update_data(forward_to=all_ids)
    await _render_forward_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == CB.POST_FWD_NONE)
async def cb_post_fwd_none(callback: CallbackQuery, state: FSMContext):
    await state.update_data(forward_to=[])
    await _render_forward_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == CB.POST_SETTINGS_DEFER)
async def cb_post_settings_defer(callback: CallbackQuery, state: FSMContext):
    # Открываем календарь «Отложить» (свернутый) и шапку с TZ/датой/постами
    data = await state.get_data()
    chan_id = int(data.get("channel_id") or 0)
    if not chan_id:
        return await callback.answer("Сначала выберите канал", show_alert=True)
    from datetime import datetime as _dt

    center = _dt.now()
    with suppress(Exception):
        await state.update_data(
            defer_center=center.date().isoformat(),
            defer_selected=center.date().isoformat(),
            defer_expanded=False,
            ui_submenu="defer",
        )
    # Заголовок и календарь из main (реюз)
    from app.bot.routers.main import _render_defer_header_text, _build_defer_calendar_kb

    text = await _render_defer_header_text(chan_id, center.date())
    kb = _build_defer_calendar_kb(chan_id, center, center, expanded=False)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
    await state.set_state(PostFSM.defer_time_input)