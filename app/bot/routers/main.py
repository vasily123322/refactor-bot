from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAnimation,
    InputMediaAudio,
    LinkPreviewOptions,
)
import asyncio
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
import html
from datetime import datetime, date, timezone, timedelta
import re
from sqlalchemy import select, func
from contextlib import suppress
from typing import Any, Mapping, Tuple
from app.core.callbacks import CB
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.settings import ChannelSettingsRepo
from app.bot.fsm.states import SettingsFSM, PostFSM
from app.bot.keyboards.posting import (
    post_actions,
    settings_menu_kb,
    create_post_card_kb,
    ad_settings_presets_kb,
)
from app.bot.keyboards.builders import build_root_settings_kb as _build_root_settings_kb
from app.bot.routers.utils.time_utils import (
    _build_calendar_kb,
    _build_defer_calendar_kb,
    _parse_time_hhmm,
)
from app.bot.keyboards.reply import add_channel_kb
from app.bot.bot_instance import bot as tg_bot
from app.core.timezone import (
    OFFSET_CITIES,
    offset_minutes_from_tz as _offset_minutes_from_tz,
)
from app.core.timezone import to_user_tz as _to_user_tz
from app.bot.routers.start import rp_add_channel, cmd_start
from app.bot.routers.content_plan import rp_content_plan_entry
from app.bot.routers.settings import rp_settings
from app.bot.routers.shared import escape_markdown_label as _escape_markdown_label
from app.core.logging import with_context_logging
from app.bot.routers.shared import safe_edit_reply_markup as _safe_edit_reply_markup
from app.bot.routers.shared import build_preview_kb as _build_preview_kb
from app.bot.routers.shared import (
    build_payload_from_message as _build_payload_from_message,
)
from app.bot.routers.shared import (
    resolve_original_post_ref as _resolve_original_post_ref,
)
from app.bot.routers.shared import (
    load_user_ui_context as _load_ui_context,
    should_show_reply_keyboard as _should_show_reply_keyboard,
    update_last_channel as _update_last_channel,
    is_ai_enabled as _is_ai_enabled,
)
from app.services.ai_generation import AIGenerationService
from app.services.post_editor.validators import validate_payload as _validate_payload
from app.services.post_editor.transforms import normalize_payload as _normalize_payload
from loguru import logger
from zoneinfo import ZoneInfo
from app.domain.models import Channel


async def _local_to_utc_for_channel(
    chan_id: int, when_local: datetime
) -> Tuple[datetime, datetime]:
    # Перевод локального времени в UTC с учётом настроек канала
    async with AsyncSessionLocal() as session:
        from app.repositories.settings import ChannelSettingsRepo

        repo = ChannelSettingsRepo(session)
        st = await repo.get_by_channel_id(chan_id)
        tz_code = None
        if st and st.filters:
            tz_code = st.filters.get("tz")
        if tz_code and tz_code.startswith("UTC"):
            mins = _offset_minutes_from_tz(tz_code)
            when_utc = when_local - timedelta(minutes=mins)
        elif tz_code:
            try:
                when_aware = when_local.replace(tzinfo=ZoneInfo(tz_code))
                when_utc = when_aware.astimezone(timezone.utc)
            except Exception:
                mins = _offset_minutes_from_tz(tz_code)
                when_utc = when_local - timedelta(minutes=mins)
        else:
            mins = 180
            when_utc = when_local - timedelta(minutes=mins)
    when_utc_aware = (
        when_utc
        if (getattr(when_utc, "tzinfo", None) is not None)
        else when_utc.replace(tzinfo=timezone.utc)
    )
    when_utc_naive = when_utc_aware.astimezone(timezone.utc).replace(tzinfo=None)
    return when_utc_aware, when_utc_naive


async def _schedule_post_to_targets(
    chan_id: int, data: dict, message: Message, when_utc_aware: datetime
) -> None:
    async with AsyncSessionLocal() as session:
        from app.services.posting import PostingService

        service = PostingService(tg_bot, session)
        payload = _payload_from(data)
        targets: list[int] = [chan_id]
        fwd: list[int] = list(data.get("forward_to") or [])
        for t in fwd:
            if t not in targets:
                targets.append(t)
        for t in targets:
            try:
                uid = int(getattr(message.from_user, "id", 0) or 0)
                if uid and not (
                    payload.get("meta") and payload["meta"].get("author_user_id")
                ):
                    payload = dict(payload)
                    payload.setdefault("meta", {})
                    payload["meta"]["author_user_id"] = uid
                    payload["meta"]["author_username"] = getattr(
                        message.from_user, "username", None
                    )
                    payload["meta"]["author_full_name"] = getattr(
                        message.from_user, "full_name", None
                    )
            except Exception:
                pass
            await service.schedule(t, payload, when_utc_aware)


async def _build_defer_confirmation(chan_id: int, when_local: datetime, data: dict):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    weekday_ru = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    ][when_local.weekday()]
    months = [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]
    date_human = f"{when_local.day} {months[when_local.month - 1]} {when_local.year} {when_local:%H:%M} ({weekday_ru})"
    chan_title_conf = None
    chan_link_conf = None
    try:
        async with AsyncSessionLocal() as session:
            ch = await session.get(Channel, chan_id)
            if ch:
                chan_title_conf = ch.title or str(ch.tg_chat_id)
                tg_chat_id = int(ch.tg_chat_id)
                chat = await tg_bot.get_chat(tg_chat_id)
                uname = getattr(chat, "username", None)
                if uname:
                    chan_link_conf = f"https://t.me/{uname}"
                else:
                    with suppress(Exception):
                        inv = await tg_bot.create_chat_invite_link(
                            chat_id=tg_chat_id,
                            name="content-plan",
                            creates_join_request=False,
                        )
                        chan_link_conf = getattr(inv, "invite_link", None)
    except Exception:
        pass
    notify = bool(data.get("notify_on", True))
    bell = "🔔" if notify else "🔕"
    label = _escape_markdown_label(chan_title_conf or "канал")
    chan_md = f"[{label}]({chan_link_conf})" if chan_link_conf else label
    text_confirm = (
        f"Публикация {bell} запланирована и будет опубликована {date_human} "
        f"в канале: {chan_md}"
    )
    btn = InlineKeyboardButton(
        text="Открыть контент‑план", callback_data=f"cp_pick_channel_{chan_id}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn]])
    return text_confirm, kb


## moved to utils.post_payload: _clip_text_len


## moved to utils.post_payload: _edit_text_with_fallback


## moved to utils.post_payload: _edit_media_with_fallback


async def _after_edit_cleanup(
    callback: CallbackQuery,
    state: FSMContext,
    data: dict,
    edit_chat_id: int,
    edit_msg_id: int,
) -> None:
    # Удалим старый предпросмотр при наличии
    prev_id = data.get("preview_msg_id")
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_id
            )
    # Применим закрепление при необходимости
    if bool(data.get("pin_on", False)):
        with suppress(Exception):
            await tg_bot.pin_chat_message(chat_id=edit_chat_id, message_id=edit_msg_id)
    # Вернёмся к карточке уведомления, если есть контекст
    ret = (await state.get_data()).get("return_to_notice")
    if isinstance(ret, dict) and ret.get("post_id") and ret.get("date"):
        try:
            from app.bot.routers.content_plan import cb_cp_open_post

            await cb_cp_open_post(callback, state)
            with suppress(TelegramBadRequest):
                await callback.answer()
        except Exception:
            pass
    else:
        with suppress(TelegramBadRequest):
            await callback.answer("Готово")
        await state.clear()


## moved to utils.post_payload: _apply_album_autosign_entities


def _build_editor_actions_kb_from_state(prev: dict) -> InlineKeyboardMarkup:
    payload = _payload_from(prev)
    for_video_note = payload.get("type") == "video_note"
    has_text = bool((payload.get("text") or payload.get("caption") or "").strip())
    kb = post_actions(
        for_video_note=for_video_note,
        has_media=payload.get("type")
        in {"photo", "video", "animation", "audio", "voice", "video_note", "album"},
        notify_on=bool(prev.get("notify_on", True)),
        autosign_on=bool(prev.get("autosign_on", False)),
        pin_on=bool(prev.get("pin_on", False)),
        comments_on=bool(prev.get("comments_on", True)),
        is_draft=bool(prev.get("is_draft", False)),
        edit_mode=(
            prev.get("edit_chat_id") is not None
            and prev.get("edit_msg_id") is not None
            and not bool(prev.get("is_draft", False))
        ),
        has_text=has_text,
        has_buttons=bool((payload.get("buttons") or [])),
        ai_enabled=_is_ai_enabled(prev),
    )
    if payload.get("buttons") or []:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        user_rows = []
        for r in payload.get("buttons"):
            row_btns = []
            for b in r:
                row_btns.append(
                    InlineKeyboardButton(text=b.get("text", "Button"), url=b.get("url"))
                )
            user_rows.append(row_btns)
        kb = InlineKeyboardMarkup(
            inline_keyboard=user_rows + (kb.inline_keyboard or [])
        )
    return kb


def _payload_from(data: Mapping[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    payload = data.get("payload")
    return dict(payload or {})


def _is_media_menu_markup(markup) -> bool:
    try:
        if markup and getattr(markup, "inline_keyboard", None):
            for row in markup.inline_keyboard:
                for btn in row:
                    data_val = getattr(btn, "callback_data", "") or ""
                    if data_val in (
                        CB.MEDIA_POS_TOGGLE,
                        CB.MEDIA_SPOILER_TOGGLE,
                        CB.MEDIA_REPLACE,
                    ):
                        return True
        return False
    except Exception:
        return False


async def _restore_preview_from_saved_payload(
    callback: CallbackQuery,
    state: FSMContext,
    data: dict,
    *,
    clear_buttons: bool = False,
    clear_media_prompt: bool = False,
) -> None:
    prev_id = data.get("preview_msg_id")
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_id
            )
    payload = _payload_from(data)
    notify_on = bool(data.get("notify_on", True))
    autosign_on = bool(data.get("autosign_on", False))
    pin_on = bool(data.get("pin_on", False))
    comments_on = bool(data.get("comments_on", True))
    preview_msg = await _send_preview_message(
        callback.message,
        payload,
        notify_on=notify_on,
        autosign_on=autosign_on,
        pin_on=pin_on,
        comments_on=comments_on,
        is_draft=bool(data.get("is_draft", False)),
        edit_mode=(
            data.get("edit_chat_id") is not None
            and data.get("edit_msg_id") is not None
            and not bool(data.get("is_draft", False))
        ),
        has_buttons=False,
        state=state,
    )
    updates = {"preview_msg_id": preview_msg.message_id}
    if clear_buttons:
        updates["buttons_prompt_ids"] = []
    if clear_media_prompt:
        updates["media_prompt_id"] = None
    await state.update_data(**updates)
    await state.set_state(PostFSM.preview)
    with suppress(TelegramBadRequest):
        await callback.answer()


async def _open_cp_card_from_restore(
    callback: CallbackQuery, state: FSMContext, restore: dict
) -> bool:
    try:
        pid = int(restore.get("post_id"))
        d = str(restore.get("date"))
        cb2 = callback.model_copy(update={"data": f"cp_open_post:{pid}:{d}"})  # type: ignore
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id, message_id=callback.message.message_id
            )
        from app.bot.routers.content_plan import cb_cp_open_post

        await cb_cp_open_post(cb2, state)
        with suppress(TelegramBadRequest):
            await callback.answer()
        with suppress(Exception):
            await state.update_data(prev_editor_restore=None)
        return True
    except Exception:
        return False


async def _handle_back_from_preview(callback: CallbackQuery, state: FSMContext) -> None:
    data_all_top = await state.get_data()
    prev_id = data_all_top.get("preview_msg_id")
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id, message_id=int(prev_id)
            )
    prev_restore = data_all_top.get("prev_editor_restore")
    if isinstance(prev_restore, dict) and prev_restore.get("type") == "cp_card":
        if await _open_cp_card_from_restore(callback, state, prev_restore):
            return
    # Если нет prev_editor_restore — просто очистим и уйдём в старт
    await state.clear()
    await cmd_start(callback.message)
    with suppress(TelegramBadRequest):
        await callback.answer()


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


# --- New: Create post card actions ---
@router.callback_query(F.data == CB.POST_TOGGLE_AD)
@with_context_logging
async def cb_post_toggle_ad(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ui_settings = dict(data.get("ui_settings") or {})
    if not ui_settings:
        toggles, _, client_id = await _load_ui_context(callback.from_user)
        ui_settings = dict(toggles)
        await state.update_data(ui_settings=ui_settings, ui_client_id=client_id)
    if not ui_settings.get("ad_posts", False):
        await state.update_data(is_ad=False)
        return await callback.answer(
            "Рекламные посты отключены в настройках интерфейса", show_alert=True
        )
    is_ad = bool(data.get("is_ad", False))
    is_ad = not is_ad
    await state.update_data(is_ad=is_ad)
    with suppress(TelegramBadRequest):
        await _safe_edit_reply_markup(
            tg_bot,
            callback.message.chat.id,
            callback.message.message_id,
            create_post_card_kb(
                ad_on=is_ad,
                ads_available=True,
                ai_available=bool(ui_settings.get("ai_compose", False)),
            ),
        )
    # Если включили рекламу — сразу открываем меню рекламного поста
    if is_ad:
        await cb_post_ad_open(callback, state)
        return
    await callback.answer("Реклама: выкл")


@router.callback_query(F.data == CB.POST_AD_OPEN)
async def cb_post_ad_open(callback: CallbackQuery, state: FSMContext):
    """Открыть меню рекламного поста: Новая бронь / Назад."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="+ Новая бронь", callback_data=CB.POST_AD_NEW)],
            [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_TOGGLE_AD)],
        ]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "<b>Рекламный пост</b>\n\nПришлите рекламный пост.\n\nЕщё нет поста или оплаты?\nЗабронируйте слот — реклама выйдет, если позже подтвердите бронь.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == CB.POST_AD_NEW)
async def cb_post_ad_new(callback: CallbackQuery, state: FSMContext):
    """Запросить имя рекламодателя и затем открыть настройки брони."""
    await state.set_state(PostFSM.ad_name_input)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_AD_OPEN)]
        ]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "<b>Новая бронь</b>\n\nПришлите имя рекламодателя. Готовый контент загрузите позже.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(PostFSM.ad_name_input)
async def on_ad_name_input(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        return await message.answer("Введите имя рекламодателя текстом")
    await state.update_data(advertiser=name)
    # Открываем настройки брони с пресетами
    with suppress(TelegramBadRequest):
        await message.answer(
            "⚙️ <b>Настройка рекламного поста</b>\n\nВыберите формат рекламы для автозаполнения значений и настройте другие параметры.",
            reply_markup=ad_settings_presets_kb(active_preset=None),
            parse_mode="HTML",
        )
    await state.set_state(PostFSM.create_card)


@router.callback_query(F.data.startswith(CB.POST_AD_PRESET_PREFIX))
async def cb_post_ad_preset(callback: CallbackQuery, state: FSMContext):
    """Установить пресет: N/top_hours.M/auto_delete_hours per screenshots.
    Keys: 1/24 -> "1_24", 2/48 -> "2_48", 3/72 -> "3_72"."""
    key = callback.data.removeprefix(CB.POST_AD_PRESET_PREFIX)
    mapping = {
        "1_24": (0, 24),  # top off, delete after 24h
        "2_48": (2, 48),  # top 2h, delete after 48h
        "3_72": (0, 72),  # top off, delete after 72h
    }
    top_hours, del_hours = mapping.get(key, (0, 24))
    # Сохраняем в payload таймер автоудаления, а «время в топе» храним маркером для планировщика/валидации
    data = await state.get_data()
    payload = _payload_from(data)
    payload["autodelete_seconds"] = int(del_hours * 3600)
    payload["autodelete_label"] = f"{del_hours}ч"
    if top_hours > 0:
        payload["priority_top_hours"] = int(top_hours)
    else:
        payload.pop("priority_top_hours", None)
    await state.update_data(payload=payload, ad_active_preset=key)
    # Перерисуем клавиатуру с обновлённой меткой «Время в топе»
    try:
        top_label = (
            f"Время в топе: {top_hours}ч" if top_hours > 0 else "Время в топе: выкл."
        )
        kb = ad_settings_presets_kb(active_preset=key)
        rows = []
        for row in kb.inline_keyboard:
            new_row = []
            for btn in row:
                if getattr(btn, "callback_data", "") == CB.POST_TOP_OPEN:
                    btn.text = top_label
                new_row.append(btn)
            rows.append(new_row)
        kb.inline_keyboard = rows
        await _safe_edit_reply_markup(
            tg_bot, callback.message.chat.id, callback.message.message_id, kb
        )
    except Exception:
        pass
    await callback.answer("Пресет применён")


@router.callback_query(F.data == CB.POST_AD_SETTINGS_OPEN)
async def cb_post_ad_settings_open(callback: CallbackQuery, state: FSMContext):
    """Открыть расширенное меню настроек брони (как на фото)."""
    data = await state.get_data()
    payload = _payload_from(data)
    timer_set = bool(payload.get("autodelete_seconds"))
    # Базово показываем то же меню настроек публикации, но заголовком «Настройка рекламного поста»
    kb = settings_menu_kb(
        timer_set=timer_set,
        repeat_on=bool(data.get("repeat_on", False)),
        time_seconds=int(payload.get("autodelete_seconds") or 0),
        views_value=int(payload.get("autodelete_views") or 0),
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "⚙️ <b>Настройка рекламного поста</b>\n\nВыберите формат рекламы для автозаполнения значений и настройте другие параметры.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == CB.POST_AD_EDIT)
async def cb_post_ad_edit(callback: CallbackQuery, state: FSMContext):
    """Вернуться в редактор (предпросмотр) из меню брони."""
    data = await state.get_data()
    payload = _payload_from(data)
    preview = await _send_preview_message(
        callback.message,
        payload,
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
        is_draft=bool(data.get("is_draft", False)),
        edit_mode=False,
        has_buttons=bool(payload.get("buttons")),
        state=state,
    )
    await state.update_data(preview_msg_id=preview.message_id)
    await state.set_state(PostFSM.preview)
    await callback.answer()


@router.callback_query(F.data == CB.POST_TOP_OPEN)
async def cb_post_top_open(callback: CallbackQuery, state: FSMContext):
    """Открыть меню выбора времени в топе (нет, 10m, 1h, 2h, 3h, 4h)."""
    data = await state.get_data()
    cur_h = int((data.get("payload") or {}).get("priority_top_hours") or 0)

    def _btn(label: str, hours: int | None = None):
        mark = "✅ " if ((hours or 0) == cur_h) else ""
        cb = CB.POST_TOP_SET_PREFIX + str((hours or 0) * 3600)
        return InlineKeyboardButton(text=(mark + label), callback_data=cb)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("нет", 0), _btn("10m", None), _btn("1h", 1)],
            [_btn("2h", 2), _btn("3h", 3), _btn("4h", 4)],
            [
                InlineKeyboardButton(
                    text="← Назад в настройки публикации",
                    callback_data=CB.POST_AD_SETTINGS_OPEN,
                )
            ],
        ]
    )
    text = (
        "<b>Время в топе</b>\n\n"
        "Установите период, в течение которого другие посты публиковаться не будут.\n"
        "Используйте кнопки или отправьте значение в удобном формате:\n\n"
        "<code>2</code> – 2 часа\n<code>30m</code> – 30 минут"
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith(CB.POST_TOP_SET_PREFIX))
async def cb_post_top_set(callback: CallbackQuery, state: FSMContext):
    try:
        secs = int(callback.data.removeprefix(CB.POST_TOP_SET_PREFIX))
    except Exception:
        secs = 0
    hours = 0 if secs <= 0 else max(0, secs // 3600)
    data = await state.get_data()
    payload = _payload_from(data)
    if hours > 0:
        payload["priority_top_hours"] = int(hours)
    else:
        payload.pop("priority_top_hours", None)
    await state.update_data(payload=payload)
    await cb_post_ad_settings_open(callback, state)


@router.callback_query(F.data == CB.POST_AI_QUICK)
async def cb_post_ai_quick(callback: CallbackQuery, state: FSMContext):
    # Переходим в быстрый режим ИИ (простая подсказка и ввод запроса)
    await state.set_state(PostFSM.ai_topic_input)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Назад", callback_data=CB.AI_BACK_TO_CREATE
                ),
                InlineKeyboardButton(text="❤️ Промпты", callback_data="ai_prompts"),
            ]
        ]
    )
    text = (
        "🧠 AI-ассистент\n\n"
        "Введите ваш запрос. Нейросеть поможет сделать рерайт, перевод или нарисует картинку к посту."
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb)
    await state.update_data(ai_menu_msg_id=callback.message.message_id)
    await callback.answer()


@router.callback_query(F.data == CB.AI_BACK_TO_CREATE)
async def cb_ai_back_to_create(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ui_settings = dict(data.get("ui_settings") or {})
    ui_client_id = data.get("ui_client_id")
    if not ui_settings or ui_client_id is None:
        toggles, _, client_id = await _load_ui_context(callback.from_user)
        ui_settings = dict(toggles)
        await state.update_data(ui_settings=ui_settings, ui_client_id=client_id)
    await state.set_state(PostFSM.create_card)
    text_card = await _build_create_post_card_text_by_channel(
        int(data.get("channel_id", 0))
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text_card,
            reply_markup=create_post_card_kb(
                ad_on=bool(data.get("is_ad", False)),
                ads_available=bool(ui_settings.get("ad_posts", False)),
                ai_available=bool(ui_settings.get("ai_compose", False)),
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await callback.answer()


# Переиспользуем существующий ИИ-пайплайн: после успешной генерации можно «Применить ответ»
# Реализовано через уже имеющиеся хендлеры generate_text/link/improve, здесь только добавили точку входа и возврат назад


@router.message(Command("remove_allrepeat"))
@with_context_logging
async def cmd_remove_allrepeat(message: Message):
    # Доступ только администратору по ADMIN_USER_ID
    try:
        from app.core.config import settings as _settings

        admin_id = getattr(_settings, "admin_user_id", None)
        if not admin_id or int(message.from_user.id) != int(admin_id):
            return await message.reply("Недоступно")
    except Exception:
        return await message.reply("Недоступно")
    # 1) Уберём из контент‑плана все pending‑задачи автоповтора (чтобы не числились)
    # 2) Снимем repeat_on и repeat_seconds у всех задач (любой статус), чтобы не генерировались новые повторы
    # 3) Очистим поля автоудаления у всех записей (autodelete_at/effective/seconds) и проставим autodeleted=True
    try:
        from sqlalchemy import select
        from app.core.db import AsyncSessionLocal
        from app.domain.models import PostTask

        removed_pending = 0
        disabled_flags = 0
        cleared_autodel = 0
        async with AsyncSessionLocal() as session:
            # Шаг 1: пометить pending‑повторы как skipped
            res_p = await session.execute(
                select(PostTask).where(PostTask.status == "pending")
            )
            pend = list(res_p.scalars().all())
            for p in pend:
                pl = dict(p.payload or {})
                if bool(pl.get("repeat_on", False)) or (
                    pl.get("repeat_group_id") is not None
                ):
                    p.status = "skipped"
                    removed_pending += 1
            await session.commit()
            # Шаг 2: снять флаги повтора везде (любой статус)
            res_all = await session.execute(select(PostTask))
            all_posts = list(res_all.scalars().all())
            for p in all_posts:
                pl = dict(p.payload or {})
                if bool(pl.get("repeat_on", False)) or ("repeat_seconds" in pl):
                    pl["repeat_on"] = False
                    pl.pop("repeat_seconds", None)
                    p.payload = pl
                    disabled_flags += 1
                # Очистка автоудаления
                if (
                    ("autodelete_at" in pl)
                    or ("autodelete_effective_seconds" in pl)
                    or ("autodelete_seconds" in pl)
                ):
                    pl.pop("autodelete_at", None)
                    pl.pop("autodelete_effective_seconds", None)
                    pl.pop("autodelete_seconds", None)
                    pl.pop("autodelete_views", None)
                    pl["autodeleted"] = True
                    pl["autodeleted_at"] = datetime.now(timezone.utc).isoformat()
                    p.payload = pl
                    cleared_autodel += 1
            await session.commit()
        return await message.reply(
            f"Готово: удалено из контент‑плана: {removed_pending}; отключено флагов повтора: {disabled_flags}; очищено автоудалений: {cleared_autodel}"
        )
    except Exception as e:
        return await message.reply(f"Ошибка: {e}")


## Перенесено в routers/settings.py: cb_post_replace_autosign_for_channel


def _register_ai_style_handlers(router):
    # Временная заглушка: регистрация AI‑стилей отключена
    return


@router.callback_query(F.data.startswith("ai_priority_"))
async def cb_ai_priority(callback: CallbackQuery):
    """Экран выбора приоритета генерации (Пресет/Пользовательский)."""
    # Обрабатываем только форму ai_priority_{cid}; игнорируем ai_priority_set_{cid}_{mode}
    parts = callback.data.split("_")
    if (
        len(parts) < 3
        or parts[1] != "priority"
        or (len(parts) >= 3 and parts[2] == "set")
    ):
        with suppress(TelegramBadRequest):
            await callback.answer()
        return
    cid = int(parts[2])
    # читаем текущий preset_id, чтобы пометить активный режим
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
    is_preset = bool(getattr(st, "preset_id", None))
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ Пресет" if is_preset else "☑️ Пресет"),
                callback_data=f"ai_priority_set_{cid}_preset",
            )
        ],
        [
            InlineKeyboardButton(
                text=("✅ Пользовательский" if not is_preset else "☑️ Пользовательский"),
                callback_data=f"ai_priority_set_{cid}_custom",
            )
        ],
        [InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")],
    ]
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Приоритет генерации",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


# После карточки «Создать пост»: если пользователь просто отправит контент — переходим в редактор, как и раньше
@router.message(PostFSM.create_card)
async def on_create_card_content(message: Message, state: FSMContext):
    # Проксируем в существующий обработчик состояния content
    await state.set_state(PostFSM.content)
    # Переотправим текущее сообщение в общий обработчик для контента
    # Здесь просто возвращаем, чтобы основной хендлер PostFSM.content поймал следующее сообщение пользователя
    # Если сообщение уже пришло — скопируем поля в state для единообразия
    payload: dict | None = None
    if message.text:
        payload = {
            "type": "text",
            "text": message.text,
            "entities": [e.model_dump() for e in (message.entities or [])],
        }
    elif message.photo:
        file_id = message.photo[-1].file_id
        payload = {
            "type": "photo",
            "file_id": file_id,
            "caption": (message.caption or None),
            "caption_entities": [
                e.model_dump() for e in (message.caption_entities or [])
            ],
        }
    elif message.video:
        payload = {
            "type": "video",
            "file_id": message.video.file_id,
            "caption": (message.caption or None),
            "caption_entities": [
                e.model_dump() for e in (message.caption_entities or [])
            ],
        }
    if payload:
        payload = _normalize_payload(payload)
        ok, err = _validate_payload(payload)
        if not ok:
            return await message.answer(f"❌ {err}")
        data = await state.get_data()
        await state.update_data(payload=payload)
        # Отрисуем предпросмотр через существующую функцию
        preview = await _send_preview_message(
            message,
            payload,
            notify_on=bool(data.get("notify_on", True)),
            autosign_on=bool(data.get("autosign_on", False)),
            pin_on=bool(data.get("pin_on", False)),
            comments_on=bool(data.get("comments_on", True)),
            is_draft=bool(data.get("is_draft", False)),
            edit_mode=False,
            has_buttons=bool(payload.get("buttons")),
            state=state,
        )
        await state.update_data(preview_msg_id=preview.message_id)
        await state.set_state(PostFSM.preview)
        return
    await message.answer("Отправьте текст или медиа для публикации")


@router.callback_query(F.data.startswith("ai_priority_set_"))
async def cb_ai_priority_set(callback: CallbackQuery):
    """Установить приоритет: preset|custom (через наличие preset_id)."""
    parts = callback.data.split("_")
    cid = int(parts[3])
    mode = parts[4]
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        if mode == "preset":
            # если уже выбран пресет — просто перерисуем; иначе попросим выбрать пресет в меню
            st = await repo.get_or_create(cid)
            if not getattr(st, "preset_id", None):
                # нет пресета — толкнём пользователя к выбору
                with suppress(TelegramBadRequest):
                    await callback.answer("Выберите пресет", show_alert=False)
            else:
                with suppress(TelegramBadRequest):
                    await callback.answer("Приоритет: Пресет")
        elif mode == "custom":
            # сброс пресета, чтобы использовался пользовательский
            await repo.update_preset(cid, None)
            with suppress(TelegramBadRequest):
                await callback.answer("Приоритет: Пользовательский")
    # перерисуем экран приоритета
    with suppress(Exception):
        await cb_ai_priority(callback)


## moved to utils/text_utils.py: convert_message_entities_to_markdown, convert_html_links_to_markdown


def _markdown_links_to_plain_and_entities(text: str) -> tuple[str, list]:
    """Convert [label](url) markdown links into plain text + text_link entities.

    Returns (plain_text, entities). Entities are dicts with type=text_link, offset, length, url.
    Only simple non-nested patterns are supported.
    """
    try:
        import re

        if not text:
            return "", []
        pattern = re.compile(r"\\?\[([^\]]+)\]\(([^)]+)\)")
        plain_parts: list[str] = []
        entities: list[dict] = []
        pos = 0
        out_len = 0
        for m in pattern.finditer(text):
            start, end = m.span()
            # If match is escaped like \[ ... ], keep literally
            escaped = text[start] == "\\"
            if escaped:
                continue
            # append non-link segment
            plain_parts.append(text[pos:start])
            out_len += len(text[pos:start])
            label = m.group(1)
            url = m.group(2)
            plain_parts.append(label)
            entities.append(
                {
                    "type": "text_link",
                    "offset": out_len,
                    "length": len(label),
                    "url": url,
                }
            )
            out_len += len(label)
            pos = end
        # tail
        plain_parts.append(text[pos:])
        plain_text = "".join(plain_parts)
        return plain_text, entities
    except Exception:
        return text or "", []


## moved to keyboards/builders.py: build_root_settings_kb (imported as _build_root_settings_kb)


# --- Local helpers to DRY repetitive blocks in this module ---
async def _get_channel_link_and_title(channel_id: int) -> tuple[str | None, str]:
    """Return (t.me link or None, channel title or fallback)."""
    chan_link = None
    chan_title = "канале"
    try:
        async with AsyncSessionLocal() as _s:
            from app.repositories.channels import ChannelsRepo as _ChRepo

            ch = await _ChRepo(_s).get_by_id(int(channel_id))
            if ch:
                try:
                    chat = await tg_bot.get_chat(int(ch.tg_chat_id))
                    uname = getattr(chat, "username", None)
                    if uname:
                        chan_link = f"https://t.me/{uname}"
                    chan_title = (
                        getattr(chat, "title", None)
                        or getattr(chat, "username", None)
                        or chan_title
                    )
                except Exception:
                    chan_link = None
    except Exception:
        chan_link = None
    return chan_link, chan_title


async def _build_create_post_card_text_by_channel(channel_id: int) -> str:
    """HTML text for the create-post card, optionally with channel link/title."""
    chan_link, chan_title = await _get_channel_link_and_title(channel_id)
    if chan_link:
        return (
            f'<b>Создать пост в <a href="{chan_link}">{chan_title}</a></b>\n\n'
            "Отправьте текст, фото, видео или другой контент, который хотите опубликовать.\n\n"
            "Поставьте галочку, если это рекламный пост:\n"
            "› Для генерации текста можно использовать AI-ассистент."
        )
    return (
        "<b>Создать пост</b>\n\n"
        "Отправьте текст, фото, видео или другой контент, который хотите опубликовать.\n\n"
        "Поставьте галочку, если это рекламный пост:\n"
        "› Для генерации текста можно использовать AI-ассистент."
    )


async def _open_create_card_for_channel(
    *,
    state: FSMContext,
    channel_id: int,
    user,
    via_message: Message | None = None,
    via_callback: CallbackQuery | None = None,
) -> None:
    data_prev = await state.get_data()
    ui_settings: dict[str, bool] = dict(data_prev.get("ui_settings") or {})
    ui_client_id = data_prev.get("ui_client_id")
    if not ui_settings or ui_client_id is None:
        toggles, _, client_id = await _load_ui_context(user)
        ui_settings = dict(toggles)
        ui_client_id = client_id
    is_draft = bool(data_prev.get("is_draft", False))
    autosign_default = False
    try:
        from app.repositories.settings import ChannelSettingsRepo as _SettingsRepo

        async with AsyncSessionLocal() as session:
            repo = _SettingsRepo(session)
            st = await repo.get_by_channel_id(channel_id)
            autosign_default = bool(st and (st.autosign or "").strip())
    except Exception:
        autosign_default = False
    await state.clear()
    await state.set_state(PostFSM.create_card)
    await state.update_data(
        ui_settings=ui_settings,
        ui_client_id=ui_client_id,
        channel_id=channel_id,
        notify_on=True,
        autosign_on=autosign_default,
        pin_on=False,
        comments_on=True,
        is_draft=is_draft,
        is_ad=False,
    )
    text_card = await _build_create_post_card_text_by_channel(channel_id)
    ads_enabled = bool(ui_settings.get("ad_posts", False))
    ai_enabled = bool(ui_settings.get("ai_compose", False))
    kb = create_post_card_kb(
        ad_on=False, ads_available=ads_enabled, ai_available=ai_enabled
    )
    if via_callback is not None:
        try:
            await via_callback.message.edit_text(
                text_card,
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            with suppress(Exception):
                await via_callback.message.answer(
                    text_card,
                    reply_markup=kb,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
    elif via_message is not None:
        await via_message.answer(
            text_card, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )
    if ui_client_id:
        if ui_settings.get("remember_channel", False):
            await _update_last_channel(user, int(ui_client_id), channel_id)
        else:
            await _update_last_channel(user, int(ui_client_id), None)


async def _render_channel_choice(
    message: Message, *, prompt_text: str, state: FSMContext | None = None
) -> None:
    """Ask user to pick a channel from their list with a Back button."""
    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        channels = ChannelsRepo(session)
        client = await clients.create_or_get(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
        )
        toggles = await clients.get_ui_settings(client.id)
        last_channel_id = await clients.get_last_channel_id(client.id)
        items = await channels.list_by_owner(client.id)
    client_id = int(client.id)
    if state is not None:
        await state.update_data(ui_settings=toggles, ui_client_id=client_id)
    if not items:
        await message.answer(
            "У вас нет каналов. Добавьте канал в главном меню → Добавить канал"
        )
        return
    valid_channels = {int(ch.id): ch for ch in items}
    remember_enabled = bool(toggles.get("remember_channel", False))
    last_id = int(last_channel_id) if last_channel_id is not None else None
    if state is not None and remember_enabled and last_id in valid_channels:
        await _open_create_card_for_channel(
            state=state, channel_id=last_id, user=message.from_user, via_message=message
        )
        return
    rows = []
    for ch in items:
        caption = (ch.title or str(ch.tg_chat_id))[:40]
        rows.append(
            [
                InlineKeyboardButton(
                    text=caption, callback_data=f"{CB.POST_PICK_CH_PREFIX}{ch.id}"
                )
            ]
        )
    kb_choose = InlineKeyboardMarkup(
        inline_keyboard=rows
        + [[InlineKeyboardButton(text="Назад", callback_data=CB.GM_GLOBAL_MENU)]]
    )
    await message.answer(prompt_text, reply_markup=kb_choose)


async def _build_pro_upsell_kb(
    message: Message, channel_id: int
) -> InlineKeyboardMarkup:
    """Build Pro upsell keyboard used in AI errors."""
    from app.core.config import settings as _settings

    admin_username = _settings.admin_username or "vasilyiusii"
    admin_url = f"https://t.me/{admin_username}"
    import urllib.parse as _urlparse

    ch_title = None
    try:
        async with AsyncSessionLocal() as _sai:
            from app.repositories.channels import ChannelsRepo as _ChRepo

            ch = await _ChRepo(_sai).get_by_id(int(channel_id))
            if ch:
                ch_title = ch.title or str(ch.tg_chat_id)
    except Exception:
        pass
    text_tpl = (
        f"Здравствуйте, пишу по поводу подписки Pro. Хочу приобрести. Канал: {ch_title or channel_id}. "
        f"Мой ник: @{message.from_user.username or ''}."
    )
    share_url = f"https://t.me/share/url?url={_urlparse.quote_plus(admin_url)}&text={_urlparse.quote_plus(text_tpl)}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оформить Pro — 990 ₽/мес", url=admin_url)],
            [InlineKeyboardButton(text="Отправить заявку", url=share_url)],
            [
                InlineKeyboardButton(
                    text="Что входит в Pro", callback_data="settings_subscription"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="ai_back_to_preview")],
        ]
    )


async def _log_ai_limit_to_admin(message: Message, channel_id: int) -> None:
    """Send a compact log line to admin log chat on AI token limit."""
    try:
        async with AsyncSessionLocal() as _slog:
            from app.repositories.admin import AdminConfigRepo as _AdminRepo
            from app.repositories.channels import ChannelsRepo as _ChRepo

            log_chat_id = await _AdminRepo(_slog).get_log_chat_id()
            if not log_chat_id:
                return
            # ссылки
            u_link = (
                f"https://t.me/{message.from_user.username}"
                if message.from_user.username
                else f"tg://user?id={message.from_user.id}"
            )
            chan_link = None
            chx = await _ChRepo(_slog).get_by_id(int(channel_id))
            if chx:
                try:
                    chat = await tg_bot.get_chat(int(chx.tg_chat_id))
                    uname = getattr(chat, "username", None)
                    if uname:
                        chan_link = f"https://t.me/{uname}"
                except Exception:
                    chan_link = None
            text_log = (
                f"AI limit hit: user={u_link} | channel={(chan_link or channel_id)}"
            )
            with suppress(Exception):
                await tg_bot.send_message(
                    int(log_chat_id), text_log, disable_web_page_preview=True
                )
    except Exception:
        pass


# Дебаунс-задачи для медиагрупп по chat_id
ALBUM_DEBOUNCE = {}


async def _render_defer_header_text(channel_id: int, day: date) -> str:
    # Получаем TZ и список постов на дату
    async with AsyncSessionLocal() as session:
        from app.repositories.settings import ChannelSettingsRepo

        repo = ChannelSettingsRepo(session)
        st = await repo.get_by_channel_id(channel_id)
        tz_code = None
        if st and st.filters:
            tz_code = st.filters.get("tz")
        mins = _offset_minutes_from_tz(tz_code)
        label = ("GMT+%d" % (mins // 60)) if mins >= 0 else ("GMT%d" % (mins // 60))
        cities = OFFSET_CITIES.get(mins, "")
        city = cities.split(",")[0] if cities else ""
        # Подтянем pending-задачи на дату
        from sqlalchemy import select
        from app.domain.models import PostTask
        from datetime import datetime as _dt, timezone

        start_utc = _dt(day.year, day.month, day.day, 0, 0, tzinfo=timezone.utc)
        end_utc = _dt(day.year, day.month, day.day, 23, 59, tzinfo=timezone.utc)
        res = await session.execute(
            select(PostTask).where(
                (PostTask.channel_id == channel_id)
                & (PostTask.status == "pending")
                & (PostTask.scheduled_at.is_not(None))
                & (PostTask.scheduled_at >= start_utc)
                & (PostTask.scheduled_at <= end_utc)
            )
        )
        posts = list(res.scalars().all())
    # Заголовок даты
    months = [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]
    date_line = f"📅 {day.day} {months[day.month - 1]} {day.year}"
    # Список постов
    if not posts:
        body = "На эту дату посты не запланированы."
    else:
        lines = []
        for p in posts:
            try:
                when = p.scheduled_at
                hm = _to_user_tz(when, "UTC").strftime("%H:%M")
                t = p.payload or {}
                if t.get("type") == "text":
                    title = (t.get("text") or "").strip().splitlines()[0][:40]
                else:
                    cap = t.get("caption") or t.get("text") or ""
                    title = cap.strip().splitlines()[0][:40]
                lines.append(f"{hm} — {title}")
            except Exception:
                lines.append("—")
        body = "\n".join(lines)
    # Подсказка времени и TZ в требуемом формате
    city_part = f" {city}" if city else ""
    tz_line = f"🕙 Укажите время публикации ({label}{city_part}) в формате: 17:30 | 17 30 | 1730"
    return f"{date_line}\n\n{body}\n\n{tz_line}"


async def _try_handle_global_reply(message: Message, state: FSMContext) -> bool:
    """Обработать кнопки reply-клавиатуры из любого состояния."""
    txt = (message.text or "").strip()
    if not txt:
        return False
    # «Главное меню» — возвращаемся в корень
    if txt == "Главное меню":
        await state.clear()
        await cmd_start(message)
        return True
    # Остальные кнопки главного меню
    mapping = {
        "Добавить канал/чат": rp_add_channel,
        "Создать пост": rp_create_post,
        "Редактировать пост": rp_edit_post_entry,
        "Черновик": rp_create_draft,
        "Контент план": rp_content_plan_entry
        if "rp_content_plan_entry" in globals()
        else None,
        "Настройки": rp_settings if "rp_settings" in globals() else None,
    }
    handler = mapping.get(txt)
    if handler is None:
        return False
    # Большинство обработчиков сами чистят состояние. На всякий случай очистим перед переходом.
    with suppress(Exception):
        await state.clear()
    # У некоторых хендлеров нет параметра state
    only_msg_handlers = (rp_add_channel, rp_content_plan_entry, rp_settings)
    if handler in only_msg_handlers:
        await handler(message)
    else:
        await handler(message, state)
    return True


## Перенесено в routers/start.py: cmd_start, rp_main_menu, rp_add_channel, rp_add_channel_alias


@router.message(F.text == "Редактировать пост")
async def rp_edit_post_entry(message: Message, state: FSMContext):
    # Вход в режим редактирования существующей публикации
    await state.clear()
    await state.set_state(PostFSM.edit_pick)
    text = (
        "Перешлите пост из вашего канала, который нужно изменить.\n\n"
        "Будет отредактирован только пересланный пост."
    )
    # Показать под текстом кнопку «Назад»
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data=CB.POST_BACK)]
        ]
    )
    await message.answer(text, reply_markup=kb)


@router.message(PostFSM.edit_pick)
async def on_edit_post_pick(message: Message, state: FSMContext):
    # Глобальные кнопки reply разрешены в любых режимах
    if await _try_handle_global_reply(message, state):
        return
    # Определить оригинальный чат и message_id пересланного поста
    orig_chat_id, orig_msg_id = await _resolve_original_post_ref(message, tg_bot)
    # Если не удалось — попросим переслать корректно
    if orig_chat_id is None or orig_msg_id is None:
        return await message.answer(
            "❌ Не удалось определить пост. Перешлите сам пост из канала, чтобы я смог его отредактировать."
        )
    # Построим payload из пересланного сообщения (копия контента)
    payload = _build_payload_from_message(
        message, prefer_html_caption=False, include_entities=True
    )
    if not payload:
        return await message.answer(
            "❌ Не получилось распознать содержимое пересланного поста"
        )
    # Найдём канал в базе по tg chat id (если есть)
    chan_id: int | None = None
    async with AsyncSessionLocal() as session:
        channels = ChannelsRepo(session)
        ch = await channels.get_by_chat_id(orig_chat_id)
        if ch:
            chan_id = int(ch.id)
    # Сохраним состояние редактирования
    await state.set_state(PostFSM.preview)
    await state.update_data(
        channel_id=chan_id or 0,
        edit_chat_id=orig_chat_id,
        edit_msg_id=orig_msg_id,
        payload=payload,
        notify_on=True,
        autosign_on=False,
        pin_on=False,
        comments_on=True,
        is_draft=False,
    )
    # Показать привычный предпросмотр и клавиатуру редактирования (режим edit)
    # Определим autosign_default из состояния выбора канала (если уже выбирали канал ранее)
    try:
        data_prev = await state.get_data()
        autosign_default = bool(data_prev.get("autosign_on", False))
    except Exception:
        autosign_default = False
    preview = await _send_preview_message(
        message,
        payload,
        notify_on=True,
        autosign_on=autosign_default,
        pin_on=False,
        comments_on=True,
        is_draft=False,
        edit_mode=True,
    )
    await state.update_data(preview_msg_id=preview.message_id)


@router.message(F.text == "Канал")
async def rp_pick_channel_request(message: Message):
    # Запускаем системный выбор КАНАЛА с нужными правами в момент нажатия
    from aiogram.types import KeyboardButtonRequestChat
    from app.bot.keyboards.reply import ChatAdministratorRights

    rights_bot_channel = ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=False,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_post_messages=True,
        can_edit_messages=True,
    )
    rights_user_channel = ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=False,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=True,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_post_messages=True,
        can_edit_messages=True,
    )
    InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть список каналов",
                    callback_data="noop",
                )
            ]
        ]
    )
    # Но Telegram не позволяет триггерить системный выбор через callback; потому отправим reply-клавиатуру с одной кнопкой
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

    rk = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Выбрать канал",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=201,
                        chat_is_channel=True,
                        chat_is_created=False,
                        bot_administrator_rights=rights_bot_channel,
                        user_administrator_rights=rights_user_channel,
                    ),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    if await _should_show_reply_keyboard(message.from_user):
        await message.answer("Выберите канал", reply_markup=rk)
    else:
        await message.answer(
            "Системный выбор каналов скрыт. Включите нижнее меню в настройках интерфейса, чтобы использовать эту функцию."
        )


@router.message(F.text == "Чат")
async def rp_pick_group_request(message: Message):
    # Запускаем системный выбор ЧАТА/ГРУППЫ с нужными правами в момент нажатия
    from aiogram.types import KeyboardButtonRequestChat
    from app.bot.keyboards.reply import ChatAdministratorRights

    rights_bot_group = ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=False,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_pin_messages=True,
    )
    rights_user_group = ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=False,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=True,
        can_promote_members=True,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_pin_messages=True,
    )
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

    rk = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Выбрать чат/группу",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=202,
                        chat_is_channel=False,
                        chat_is_created=False,
                        bot_administrator_rights=rights_bot_group,
                        user_administrator_rights=rights_user_group,
                    ),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    if await _should_show_reply_keyboard(message.from_user):
        await message.answer("Выберите чат/группу", reply_markup=rk)
    else:
        await message.answer(
            "Системный выбор чатов скрыт. Включите нижнее меню в настройках интерфейса, чтобы использовать эту функцию."
        )


@router.message(F.text == "Добавить канал")
async def rp_add_channel_alias(message: Message):
    await rp_add_channel(message)


@router.message(F.text == "Создать пост")
async def rp_create_post(message: Message, state: FSMContext):
    # Сначала предложим выбрать канал/чат для будущей публикации
    await state.clear()
    await _render_channel_choice(
        message, prompt_text="Выберите канал для создания публикации", state=state
    )


@router.message(F.text == "Черновик")
async def rp_create_draft(message: Message, state: FSMContext):
    # Как «Создать пост», но в режиме черновика (без публикации)
    await state.clear()
    # Флаг draft в состоянии
    await state.update_data(is_draft=True)
    await _render_channel_choice(
        message, prompt_text="Выберите канал для черновика", state=state
    )


# Обработчик выбора канала для создания поста
@router.callback_query(F.data.startswith(CB.POST_PICK_CH_PREFIX))
async def cb_post_pick_channel(callback: CallbackQuery, state: FSMContext):
    # Ожидаем формат post_pick_ch_{id}
    try:
        cid = int(callback.data.removeprefix(CB.POST_PICK_CH_PREFIX))
    except Exception:
        return await callback.answer("Ошибка данных", show_alert=True)
    await _open_create_card_for_channel(
        state=state, channel_id=cid, user=callback.from_user, via_callback=callback
    )
    await callback.answer()


# --- Редактор поста: ввод контента и предпросмотр ---
async def _send_preview_message(
    message: Message,
    payload: dict,
    *,
    notify_on: bool,
    autosign_on: bool,
    pin_on: bool,
    comments_on: bool,
    is_draft: bool,
    edit_mode: bool = False,
    has_buttons: bool = False,
    state: FSMContext | None = None,
) -> Message:
    """Отправить предпросмотр в ЛС пользователя с клавиатурой действий.
    Возвращает сообщение предпросмотра (Message)."""
    for_video_note = payload.get("type") == "video_note"
    # Определим наличие текста
    has_text = False
    t = payload.get("type")
    if t == "text":
        has_text = bool((payload.get("text") or "").strip())
    elif t in {"photo", "video", "animation", "audio", "voice", "video_note", "album"}:
        has_text = bool((payload.get("caption") or "").strip())
    # Определим наличие кнопок у поста
    has_buttons = bool((payload.get("buttons") or []))
    ai_enabled = False
    if state is not None:
        try:
            ai_enabled = _is_ai_enabled(await state.get_data())
        except Exception:
            ai_enabled = False
    base_kb = post_actions(
        for_video_note=for_video_note,
        has_media=payload.get("type")
        in {"photo", "video", "animation", "audio", "voice", "video_note", "album"},
        notify_on=notify_on,
        autosign_on=autosign_on,
        pin_on=pin_on,
        comments_on=comments_on,
        is_draft=is_draft,
        edit_mode=edit_mode,
        has_text=has_text,
        has_buttons=has_buttons,
        ai_enabled=ai_enabled,
    )
    # Не добавляем дополнительную «Назад» — возврат обрабатывает POST_BACK с сохранённым контекстом
    type_ = payload.get("type")
    if type_ == "album":
        # Отправим медиагруппу (или платную медиагруппу), затем отдельные сообщения: сначала пользовательские кнопки (если есть), ниже — «Редактор альбома» с клавиатурой редактора
        items: list[dict] = list(payload.get("items") or [])
        media_group = []
        for idx, it in enumerate(items):
            t = it.get("type")
            fid = it.get("file_id")
            cap = it.get("caption") or None
            cent = it.get("caption_entities") or None
            # Подпись прикрепляем только к последнему элементу
            cap_to_use = cap if (idx == len(items) - 1 and cap) else None
            cent_to_use = cent if (idx == len(items) - 1 and cent) else None
            # Клип по лимиту Telegram для caption (1024), при обрезке убираем entities
            if cap_to_use and len(cap_to_use) > 1024:
                cap_to_use = cap_to_use[:1023] + "…"
                cent_to_use = None
            if t == "photo":
                m = InputMediaPhoto(
                    media=fid,
                    caption=cap_to_use,
                    parse_mode=(None if cent_to_use else "Markdown"),
                )
                if cent_to_use:
                    m.caption_entities = cent_to_use
            elif t == "video":
                m = InputMediaVideo(
                    media=fid,
                    caption=cap_to_use,
                    parse_mode=(None if cent_to_use else "Markdown"),
                )
                if cent_to_use:
                    m.caption_entities = cent_to_use
            elif t == "animation":
                m = InputMediaAnimation(
                    media=fid,
                    caption=cap_to_use,
                    parse_mode=(None if cent_to_use else "Markdown"),
                )
                if cent_to_use:
                    m.caption_entities = cent_to_use
            elif t == "audio":
                m = InputMediaAudio(
                    media=fid,
                    caption=cap_to_use,
                    parse_mode=(None if cent_to_use else "Markdown"),
                )
                if cent_to_use:
                    m.caption_entities = cent_to_use
            else:
                continue
            media_group.append(m)
        if media_group:
            with suppress(TelegramBadRequest):
                # Если включена платность и задана цена — отправим платную группу (photo/video)
                paid_on = bool(payload.get("media_paid_on", False))
                price = int(payload.get("media_paid_price") or 0)
                if paid_on and price > 0:
                    try:
                        from aiogram.types import (
                            InputPaidMediaPhoto,
                            InputPaidMediaVideo,
                        )
                    except Exception:
                        InputPaidMediaPhoto = None  # type: ignore
                        InputPaidMediaVideo = None  # type: ignore
                    paid_media = []
                    for it in items:
                        if (
                            it.get("type") == "photo"
                            and InputPaidMediaPhoto is not None
                        ):
                            paid_media.append(
                                InputPaidMediaPhoto(media=it.get("file_id"))
                            )
                        elif (
                            it.get("type") == "video"
                            and InputPaidMediaVideo is not None
                        ):
                            paid_media.append(
                                InputPaidMediaVideo(media=it.get("file_id"))
                            )
                    if paid_media:
                        # Подпись — как и раньше: у последнего
                        cap_to_use = None
                        cent_to_use = None
                        for idx2, it2 in enumerate(items):
                            if (it2.get("caption") or "").strip():
                                cap_to_use = it2.get("caption")
                                cent_to_use = it2.get("caption_entities")
                        # Отправим платную медиагруппу одной картой (Telegram сам добавит оверлей)
                        pm = await tg_bot.send_paid_media(
                            chat_id=message.chat.id,
                            star_count=price,
                            media=paid_media,
                            caption=cap_to_use,
                            parse_mode=(None if cent_to_use else "Markdown"),
                            caption_entities=cent_to_use,
                        )
                        res = []  # платный пост отправляется одним сообщением; ниже мы всё равно рендерим карточку редактора
                        if state is not None and pm is not None:
                            with suppress(Exception):
                                await state.update_data(
                                    preview_media_id=getattr(pm, "message_id", None)
                                )
                    else:
                        res = await tg_bot.send_media_group(
                            chat_id=message.chat.id, media=media_group
                        )
                else:
                    res = await tg_bot.send_media_group(
                        chat_id=message.chat.id, media=media_group
                    )
                # Сохраним id сообщений альбома, чтобы можно было удалить при «Изменить медиа»
                if state is not None and res:
                    try:
                        ids = [m.message_id for m in res]
                        await state.update_data(preview_album_ids=ids)
                    except Exception:
                        pass
        # Сформируем клавиатуру предпросмотра: пользовательские кнопки сверху, редактор — ниже
        kb_album = base_kb
        if payload.get("buttons") or []:
            user_rows = []
            for r in payload.get("buttons"):
                row_btns = []
                for b in r:
                    row_btns.append(
                        InlineKeyboardButton(
                            text=b.get("text", "Button"), url=b.get("url")
                        )
                    )
                user_rows.append(row_btns)
            kb_album = InlineKeyboardMarkup(
                inline_keyboard=user_rows + (base_kb.inline_keyboard or [])
            )
        # Отправим заголовок редактора альбома с итоговой клавиатурой
        text = "Редактор альбома"
        return await message.answer(
            text,
            reply_markup=kb_album,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    # Для не-альбомов очистим сохранённые id медиагруппы
    if state is not None:
        with suppress(Exception):
            await state.update_data(preview_album_ids=[])
    # Для не-альбомов: скомбинируем пользовательские кнопки поверх клавиатуры редактора
    final_kb = base_kb
    if payload.get("buttons") or []:
        user_rows = []
        for r in payload.get("buttons") or []:
            row_btns = []
            for b in r:
                row_btns.append(
                    InlineKeyboardButton(text=b.get("text", "Button"), url=b.get("url"))
                )
            user_rows.append(row_btns)
        final_kb = InlineKeyboardMarkup(
            inline_keyboard=user_rows + (base_kb.inline_keyboard or [])
        )
    if type_ == "text":
        ents = payload.get("entities")
        text_val = payload.get("text", "")
        # Сначала отправим сам текст поста отдельным сообщением
        m_txt = None
        with suppress(TelegramBadRequest):
            # По умолчанию превью ссылок отключено; включаем только из меню «Превью»
            show_on = False
            show_above = True
            try:
                if state is not None:
                    data_state = await state.get_data()
                    # Включаем, только если явно разрешено
                    show_on = bool(data_state.get("preview_show_on", False))
                    show_above = bool(data_state.get("preview_show_above", True))
            except Exception:
                pass
            if show_on:
                # Спрячем ссылку: найдём url (из payload.preview_url или из текста) и инжектим невидимую ссылку
                url = str((payload.get("preview_url") or "")).strip()
                if not url:
                    import re as _re

                    m = _re.search(r"https?://\S+", text_val)
                    url = m.group(0) if m else ""
                    if url:
                        text_val = text_val.replace(url, "").strip()
                invisible = "\u2061"  # INVISIBLE SEPARATOR
                if url:
                    html_text = (
                        f"<a href='{url}'>" + invisible + "</a>" + (text_val or "")
                    )
                    lpo = LinkPreviewOptions(
                        is_disabled=False, show_above_text=show_above
                    )
                    m_txt = await message.answer(
                        html_text,
                        parse_mode="HTML",
                        link_preview_options=lpo,
                        disable_web_page_preview=False,
                    )
                else:
                    # Если URL нет, просто отправим текст без превью
                    m_txt = await message.answer(
                        text_val,
                        parse_mode=(None if ents else "Markdown"),
                        entities=ents,
                        disable_web_page_preview=True,
                    )
            else:
                m_txt = await message.answer(
                    text_val,
                    parse_mode=(None if ents else "Markdown"),
                    entities=ents,
                    disable_web_page_preview=True,
                )
        # Сохраним id текстового сообщения предпросмотра, чтобы уметь удалить его при добавлении медиа
        if state is not None and m_txt is not None:
            with suppress(Exception):
                await state.update_data(
                    preview_text_id=getattr(m_txt, "message_id", None)
                )
        # Затем — карточку редактора как на макете
        instr = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
        return await message.answer(
            instr,
            reply_markup=final_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    elif type_ == "photo":
        cap = payload.get("caption") or None
        cent = payload.get("caption_entities")
        # Telegram допускает 0..1024 символа после парсинга
        if cap and len(cap) > 1024:
            cap = cap[:1023] + "…"
            cent = None
        # Если платный предпросмотр — отправляем реальное платное медиа через sendPaidMedia
        paid_on = bool(payload.get("media_paid_on", False))
        price = int(payload.get("media_paid_price") or 0)
        if paid_on and price > 0:
            try:
                from aiogram.types import InputPaidMediaPhoto

                pm = await tg_bot.send_paid_media(
                    chat_id=message.chat.id,
                    star_count=price,
                    media=[InputPaidMediaPhoto(media=payload.get("file_id"))],
                    caption=cap,
                    parse_mode=(None if cent else "Markdown"),
                    caption_entities=cent,
                )
                if state is not None and pm is not None:
                    with suppress(Exception):
                        await state.update_data(
                            preview_media_id=getattr(pm, "message_id", None)
                        )
            except Exception:
                # Фолбэк на обычное фото, чтобы не ломать UX предпросмотра
                m = await message.answer_photo(
                    payload.get("file_id"),
                    caption=cap,
                    reply_markup=None,
                    parse_mode=(None if cent else "Markdown"),
                    caption_entities=cent,
                )
                if state is not None and m is not None:
                    with suppress(Exception):
                        await state.update_data(
                            preview_media_id=getattr(m, "message_id", None)
                        )
        else:
            # Обычное фото
            m = await message.answer_photo(
                payload.get("file_id"),
                caption=cap,
                reply_markup=None,
                parse_mode=(None if cent else "Markdown"),
                caption_entities=cent,
            )
            if state is not None and m is not None:
                with suppress(Exception):
                    await state.update_data(
                        preview_media_id=getattr(m, "message_id", None)
                    )
        instr = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
        m2 = await message.answer(instr, reply_markup=final_kb, parse_mode="HTML")
        return m2
    elif type_ == "video":
        cap = payload.get("caption") or None
        cent = payload.get("caption_entities")
        if cap and len(cap) > 1024:
            cap = cap[:1023] + "…"
            cent = None
        paid_on = bool(payload.get("media_paid_on", False))
        price = int(payload.get("media_paid_price") or 0)
        if paid_on and price > 0:
            try:
                from aiogram.types import InputPaidMediaVideo

                pm = await tg_bot.send_paid_media(
                    chat_id=message.chat.id,
                    star_count=price,
                    media=[InputPaidMediaVideo(media=payload.get("file_id"))],
                    caption=cap,
                    parse_mode=(None if cent else "Markdown"),
                    caption_entities=cent,
                )
                if state is not None and pm is not None:
                    with suppress(Exception):
                        await state.update_data(
                            preview_media_id=getattr(pm, "message_id", None)
                        )
            except Exception:
                m = await message.answer_video(
                    payload.get("file_id"),
                    caption=cap,
                    reply_markup=None,
                    parse_mode=(None if cent else "Markdown"),
                    caption_entities=cent,
                )
                if state is not None and m is not None:
                    with suppress(Exception):
                        await state.update_data(
                            preview_media_id=getattr(m, "message_id", None)
                        )
        else:
            m = await message.answer_video(
                payload.get("file_id"),
                caption=cap,
                reply_markup=None,
                parse_mode=(None if cent else "Markdown"),
                caption_entities=cent,
            )
            if state is not None and m is not None:
                with suppress(Exception):
                    await state.update_data(
                        preview_media_id=getattr(m, "message_id", None)
                    )
        instr = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
        m2 = await message.answer(instr, reply_markup=final_kb, parse_mode="HTML")
        return m2
    elif type_ == "animation":
        cap = payload.get("caption") or None
        cent = payload.get("caption_entities")
        if cap and len(cap) > 1024:
            cap = cap[:1023] + "…"
            cent = None
        m = await message.answer_animation(
            payload.get("file_id"),
            caption=cap,
            reply_markup=None,
            parse_mode=(None if cent else "Markdown"),
            caption_entities=cent,
        )
        instr = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
        m2 = await message.answer(instr, reply_markup=final_kb, parse_mode="HTML")
        return m2
    elif type_ == "audio":
        cap = payload.get("caption") or None
        cent = payload.get("caption_entities")
        if cap and len(cap) > 1024:
            cap = cap[:1023] + "…"
            cent = None
        m = await message.answer_audio(
            payload.get("file_id"),
            caption=cap,
            reply_markup=None,
            parse_mode=(None if cent else "Markdown"),
            caption_entities=cent,
        )
        instr = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
        m2 = await message.answer(instr, reply_markup=final_kb, parse_mode="HTML")
        return m2
    elif type_ == "voice":
        cap = payload.get("caption") or None
        cent = payload.get("caption_entities")
        if cap and len(cap) > 1024:
            cap = cap[:1023] + "…"
            cent = None
        m = await message.answer_voice(
            payload.get("file_id"),
            caption=cap,
            reply_markup=None,
            parse_mode=(None if cent else "Markdown"),
            caption_entities=cent,
        )
        instr = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно внести исправления, пришлите новый текст — он заменит старый. Чтобы добавить фото или видео, просто отправьте их боту."
        m2 = await message.answer(instr, reply_markup=final_kb, parse_mode="HTML")
        return m2
    elif type_ == "video_note":
        # Telegram не поддерживает инлайн-клавиатуру у video_note, поэтому отправим pair: кружок + текст-превью
        await message.answer_video_note(payload.get("file_id"))
        text = payload.get("caption") or "Предпросмотр"
        cent = payload.get("caption_entities")
        m2 = await message.answer(
            text,
            reply_markup=final_kb,
            parse_mode=(None if cent else "Markdown"),
            entities=cent,
            disable_web_page_preview=True,
        )
        return m2
    else:
        # Вместо ошибки оставим текстовый предпросмотр с клавиатурой
        text = payload.get("text") or payload.get("caption") or "Предпросмотр"
        return await message.answer(
            text,
            reply_markup=final_kb,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )


@router.message(PostFSM.content)
async def on_post_content(message: Message, state: FSMContext):
    # Глобальные кнопки reply разрешены в любых режимах
    if await _try_handle_global_reply(message, state):
        return
    """Получение пользовательского контента и вывод предпросмотра с клавиатурой действий."""
    # Сконструируем payload
    payload: dict
    if message.photo:
        ph = message.photo[-1]
        payload = {
            "type": "photo",
            "file_id": ph.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.video:
        payload = {
            "type": "video",
            "file_id": message.video.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.animation:
        payload = {
            "type": "animation",
            "file_id": message.animation.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.audio:
        payload = {
            "type": "audio",
            "file_id": message.audio.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.voice:
        payload = {
            "type": "voice",
            "file_id": message.voice.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.video_note:
        payload = {
            "type": "video_note",
            "file_id": message.video_note.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.text or message.html_text:
        # Если ранее был медиапост, переносим текст в caption
        prev = await state.get_data()
        prev_payload = prev.get("payload") or {}
        text_val = message.text or ""
        if prev_payload.get("type") in {
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "video_note",
        }:
            payload = dict(prev_payload)
            payload["caption"] = text_val
            if getattr(message, "entities", None):
                payload["caption_entities"] = [
                    e.model_dump() for e in (message.entities or [])
                ]
        elif prev_payload.get("type") == "album":
            # Введён текст после альбома — перенесём его в подпись последнего элемента альбома
            payload = dict(prev_payload)
            items = list(payload.get("items") or [])
            if items:
                items[-1]["caption"] = text_val
                if getattr(message, "entities", None):
                    items[-1]["caption_entities"] = [
                        e.model_dump() for e in (message.entities or [])
                    ]
                else:
                    items[-1]["caption_entities"] = None
            payload["items"] = items
            payload["caption"] = text_val
        else:
            payload = {"type": "text", "text": text_val}
            if getattr(message, "entities", None):
                payload["entities"] = [e.model_dump() for e in (message.entities or [])]
    else:
        return await message.answer("❌ Пришлите текст или медиа для публикации")

    # Сборка альбома при приходе нескольких медиа подряд
    mgid = getattr(message, "media_group_id", None)
    if mgid and payload.get("type") in {"photo", "video", "animation", "audio"}:
        st = await state.get_data()
        cur_group = st.get("album_group_id")
        items = list(st.get("album_items") or [])
        if cur_group != mgid:
            items = []
        items.append(
            {
                "type": payload.get("type"),
                "file_id": payload.get("file_id"),
                "caption": payload.get("caption"),
                "caption_entities": payload.get("caption_entities"),
            }
        )
        await state.update_data(album_group_id=mgid, album_items=items)
        # Дебаунсим, чтобы не плодить несколько предпросмотров
        old_task = ALBUM_DEBOUNCE.get(message.chat.id)
        if old_task and not old_task.done():
            old_task.cancel()

        async def _finalize():
            try:
                await asyncio.sleep(0.6)
                await _finalize_album_preview(message, state)
            except asyncio.CancelledError:
                return

        ALBUM_DEBOUNCE[message.chat.id] = asyncio.create_task(_finalize())
        return

    data = await state.get_data()
    # Если канал ещё не выбран, вернём к шагу выбора канала
    if not data.get("channel_id"):
        return await message.answer(
            "Сначала выберите канал для публикации через кнопку 'Создать пост' → выбор канала"
        )
    # Если добавили медиа к ранее введённому тексту — используем текст как caption
    prev_payload2 = data.get("payload") or {}
    if payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "video_note",
    }:
        # Если до этого был текст — ничего не переносим в подпись; используем только новый caption (или пусто)
        if prev_payload2.get("type") == "text":
            pass
        elif (
            prev_payload2.get("type")
            in {"photo", "video", "animation", "audio", "voice", "video_note", "album"}
            and (prev_payload2.get("caption") or "").strip()
        ):
            # Замена медиа без подписи: переносим старую подпись
            payload["caption"] = prev_payload2.get("caption")
            if prev_payload2.get("caption_entities"):
                payload["caption_entities"] = list(
                    prev_payload2.get("caption_entities") or []
                )
    # Инициализируем флаги по умолчанию, если отсутствуют
    notify_on = bool(data.get("notify_on", True))
    autosign_on = bool(data.get("autosign_on", False))
    pin_on = bool(data.get("pin_on", False))
    comments_on = bool(data.get("comments_on", True))
    await state.update_data(payload=payload)
    # Если предпросмотр уже есть — попробуем редактировать контент, иначе отправим новый
    prev_id = data.get("preview_msg_id")
    # Если ранее показывался альбом, а мы обновляем его подпись — удалим старые сообщения медиагруппы
    if prev_payload2.get("type") == "album":
        album_ids = data.get("preview_album_ids") or []
        if album_ids:
            for mid in album_ids:
                with suppress(TelegramBadRequest):
                    await tg_bot.delete_message(chat_id=message.chat.id, message_id=mid)
            await state.update_data(preview_album_ids=[])
    if prev_id:
        # После получения медиа: удаляем текущее сообщение (инструкцию/старый предпросмотр)
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(chat_id=message.chat.id, message_id=prev_id)
        # Если ранее был отправлен текст поста отдельным сообщением — удалим его, чтобы медиа заменило предыдущее
        text_id = data.get("preview_text_id")
        if text_id and payload.get("type") in {
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "video_note",
            "album",
        }:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=message.chat.id, message_id=int(text_id)
                )
            with suppress(Exception):
                await state.update_data(preview_text_id=None)
        preview = await _send_preview_message(
            message,
            payload,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=bool(data.get("is_draft", False)),
            edit_mode=(
                data.get("edit_chat_id") is not None
                and data.get("edit_msg_id") is not None
                and not bool(data.get("is_draft", False))
            ),
            has_buttons=False,
            state=state,
        )
        await state.update_data(preview_msg_id=preview.message_id)
    else:
        preview = await _send_preview_message(
            message,
            payload,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=bool(data.get("is_draft", False)),
            edit_mode=(
                data.get("edit_chat_id") is not None
                and data.get("edit_msg_id") is not None
                and not bool(data.get("is_draft", False))
            ),
            has_buttons=False,
            state=state,
        )
        await state.update_data(preview_msg_id=preview.message_id)
    await state.set_state(PostFSM.preview)
    # Удалим служебные приглашения, если были: для шага «Отправьте или перешлите …»
    state_data_after = await state.get_data()
    content_prompt_id = state_data_after.get("content_prompt_id")
    if content_prompt_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=message.chat.id, message_id=content_prompt_id
            )
        await state.update_data(content_prompt_id=None)


async def _finalize_album_preview(message: Message, state: FSMContext):
    """Финализировать накопленный альбом: отправить медиагруппу и один предпросмотр ниже."""
    data = await state.get_data()
    items: list[dict] = list(data.get("album_items") or [])
    if not items:
        return
    prev_payload2 = data.get("payload") or {}
    caption = items[-1].get("caption") or ""
    cap_entities = items[-1].get("caption_entities") or None
    # Если подписи на последнем нет — попробуем взять с любого элемента альбома (частый кейс: подпись у первого фото)
    if not caption:
        for it in items:
            if (it.get("caption") or "").strip():
                caption = it.get("caption") or ""
                cap_entities = it.get("caption_entities") or None
                break
    # Если всё ещё нет — попробуем взять из предыдущего payload (текст до/после альбома)
    if not caption:
        if (
            prev_payload2.get("type") == "text"
            and (prev_payload2.get("text") or "").strip()
        ):
            caption = prev_payload2.get("text")
            if not cap_entities and prev_payload2.get("entities"):
                cap_entities = list(prev_payload2.get("entities") or [])
        elif (
            prev_payload2.get("type")
            in {"photo", "video", "animation", "audio", "voice", "video_note"}
            and (prev_payload2.get("caption") or "").strip()
        ):
            caption = prev_payload2.get("caption")
            if not cap_entities and prev_payload2.get("caption_entities"):
                cap_entities = list(prev_payload2.get("caption_entities") or [])
    # Обеспечим подпись только у последнего
    for idx in range(len(items)):
        items[idx]["caption"] = caption if idx == len(items) - 1 and caption else ""
        items[idx]["caption_entities"] = (
            cap_entities if idx == len(items) - 1 and cap_entities else None
        )
    # Сохраним пользовательские кнопки из предыдущего payload, чтобы не перенастраивать их для альбома
    prev_buttons = prev_payload2.get("buttons")
    payload = {"type": "album", "items": items, "caption": caption}
    if prev_buttons:
        payload["buttons"] = prev_buttons
    await state.update_data(payload=payload, album_items=[], album_group_id=None)
    # Удалим прошлый предпросмотр/инструкцию
    data2 = await state.get_data()
    prev_id = data2.get("preview_msg_id")
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(chat_id=message.chat.id, message_id=prev_id)
    # Если ранее был отправлен отдельный текст — удалим его при финализации альбома
    text_id2 = data2.get("preview_text_id")
    if text_id2:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=message.chat.id, message_id=int(text_id2)
            )
    with suppress(Exception):
        await state.update_data(preview_text_id=None)
    # Отправим новый предпросмотр
    notify_on = bool(data2.get("notify_on", True))
    autosign_on = bool(data2.get("autosign_on", False))
    pin_on = bool(data2.get("pin_on", False))
    comments_on = bool(data2.get("comments_on", True))
    preview = await _send_preview_message(
        message,
        payload,
        notify_on=notify_on,
        autosign_on=autosign_on,
        pin_on=pin_on,
        comments_on=comments_on,
        is_draft=bool(data2.get("is_draft", False)),
        edit_mode=(
            data2.get("edit_chat_id") is not None
            and data2.get("edit_msg_id") is not None
            and not bool(data2.get("is_draft", False))
        ),
        has_buttons=False,
        state=state,
    )
    await state.update_data(preview_msg_id=preview.message_id)
    await state.set_state(PostFSM.preview)
    # Удалим приглашение выбора контента
    content_prompt_id = data2.get("content_prompt_id")
    if content_prompt_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=message.chat.id, message_id=content_prompt_id
            )
        await state.update_data(content_prompt_id=None)


@router.message(PostFSM.preview)
async def on_preview_add_content(message: Message, state: FSMContext):
    """Разрешаем добавлять медиа/текст прямо из предпросмотра без нажатия кнопок.
    - Если был текст и пришло медиа — переносим текст в подпись к медиа (+ существующую подпись ниже).
    - Если пришёл новый текст — заменяем текст поста/подпись.
    """
    data = await state.get_data()
    prev_payload = _payload_from(data)
    new_payload: dict | None = None

    if message.photo:
        ph = message.photo[-1]
        new_payload = {
            "type": "photo",
            "file_id": ph.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            new_payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.video:
        new_payload = {
            "type": "video",
            "file_id": message.video.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            new_payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.animation:
        new_payload = {
            "type": "animation",
            "file_id": message.animation.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            new_payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.audio:
        new_payload = {
            "type": "audio",
            "file_id": message.audio.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            new_payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.voice:
        new_payload = {
            "type": "voice",
            "file_id": message.voice.file_id,
            "caption": (message.caption or ""),
        }
        if getattr(message, "caption_entities", None):
            new_payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
    elif message.text or message.html_text:
        txt = message.text or ""
        if prev_payload.get("type") in {
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "video_note",
        }:
            # заменим/добавим подпись у текущего медиа
            pp = dict(prev_payload)
            pp["caption"] = txt
            if getattr(message, "entities", None):
                pp["caption_entities"] = [
                    e.model_dump() for e in (message.entities or [])
                ]
            new_payload = pp
        else:
            # Если мы находимся в подменю превью, воспринимаем приходящий текст как ссылку для превью
            st_all = await state.get_data()
            if (
                st_all.get("ui_submenu") == "preview"
                and prev_payload.get("type") == "text"
            ):
                import re as _re

                url = None
                m = _re.search(r"https?://\S+", txt)
                if m:
                    url = m.group(0)
                if url:
                    pp = dict(prev_payload)
                    pp["preview_url"] = url
                    # Автовключим превью
                    with suppress(Exception):
                        await state.update_data(preview_show_on=True)
                        pp["preview_show_on"] = True
                    new_payload = pp
                else:
                    # Нет URL — не трогаем существующий текст
                    new_payload = dict(prev_payload)
            else:
                new_payload = {"type": "text", "text": txt}
                if getattr(message, "entities", None):
                    new_payload["entities"] = [
                        e.model_dump() for e in (message.entities or [])
                    ]
    else:
        return

    # Если прислали МЕДИА в подменю «Превью» для текстового поста — не превращаем пост в медиа
    try:
        st_all2 = await state.get_data()
        if (
            st_all2.get("ui_submenu") == "preview"
            and prev_payload.get("type") == "text"
            and new_payload
            and new_payload.get("type")
            in {"photo", "video", "animation", "audio", "voice", "video_note", "album"}
        ):
            await message.answer(
                "Для превью добавьте ссылку. Медиа в превью не поддерживается."
            )
            return
    except Exception:
        pass

    # Запрещаем перенос старого текста в подпись: используем только новый caption (или оставляем пустым)
    if (
        new_payload
        and new_payload.get("type")
        in {"photo", "video", "animation", "audio", "voice", "video_note"}
        and prev_payload.get("type") == "text"
    ):
        # Никаких изменений: new_payload["caption"] остаётся как прислал пользователь (или пусто)
        pass

    # Обновим предпросмотр
    await state.update_data(payload=new_payload)
    prev_id = data.get("preview_msg_id")
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=message.chat.id, message_id=int(prev_id)
            )
    for mid in data.get("preview_album_ids") or []:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(chat_id=message.chat.id, message_id=int(mid))
    await state.update_data(preview_album_ids=[])
    # Удалим также предыдущее текстовое сообщение предпросмотра, если есть, когда пришло новое медиа
    if new_payload and new_payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "video_note",
        "album",
    }:
        text_id = data.get("preview_text_id")
        if text_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=message.chat.id, message_id=int(text_id)
                )
            with suppress(Exception):
                await state.update_data(preview_text_id=None)

    preview = await _send_preview_message(
        message,
        new_payload,
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
        is_draft=bool(data.get("is_draft", False)),
        edit_mode=False,
        has_buttons=bool(new_payload.get("buttons")),
        state=state,
    )
    await state.update_data(preview_msg_id=preview.message_id)
    await state.set_state(PostFSM.preview)
    # Очистим дебаунс
    ALBUM_DEBOUNCE.pop(message.chat.id, None)


# --- Тумблеры предпросмотра ---
async def _edit_preview_kb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = data.get("payload", {})
    # Если в режиме ввода кнопок — показываем только пользовательские кнопки
    cur_state = await state.get_state()
    kb: InlineKeyboardMarkup | None
    if cur_state == PostFSM.buttons.state:
        btns = payload.get("buttons") or []
        if btns:
            user_rows = []
            for r in btns:
                row_btns = []
                for b in r:
                    row_btns.append(
                        InlineKeyboardButton(
                            text=b.get("text", "Button"), url=b.get("url")
                        )
                    )
                user_rows.append(row_btns)
            kb = InlineKeyboardMarkup(inline_keyboard=user_rows)
        else:
            kb = None
    else:
        # Соберём через общий хелпер
        kb = await _build_preview_kb(data)
    # Предпочтительно обновляем клавиатуру у сообщения предпросмотра
    prev_id = (await state.get_data()).get("preview_msg_id")
    if prev_id:
        await _safe_edit_reply_markup(tg_bot, callback.message.chat.id, prev_id, kb)
    else:
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise


async def _apply_preview_kb_for_chat(chat_id: int, state: FSMContext):
    """Обновить inline-клавиатуру предпросмотра по chat_id без пересоздания сообщения."""
    data = await state.get_data()
    payload = data.get("payload", {})
    for_video_note = payload.get("type") == "video_note"
    edit_mode = (
        data.get("edit_chat_id") is not None
        and data.get("edit_msg_id") is not None
        and not bool(data.get("is_draft", False))
    )
    has_text = False
    if payload.get("type") == "text":
        has_text = bool((payload.get("text") or "").strip())
    elif payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "video_note",
        "album",
    }:
        has_text = bool((payload.get("caption") or "").strip())
    has_buttons = bool((payload.get("buttons") or []))
    base_kb = post_actions(
        for_video_note=for_video_note,
        has_media=payload.get("type")
        in {"photo", "video", "animation", "audio", "voice", "video_note", "album"},
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
        is_draft=bool(data.get("is_draft", False)),
        edit_mode=edit_mode,
        has_text=has_text,
        has_buttons=has_buttons,
        ai_enabled=_is_ai_enabled(data),
    )
    # Пользовательские кнопки сверху, редактор — ниже
    kb = base_kb
    if has_buttons:
        user_rows = []
        for r in payload.get("buttons") or []:
            row_btns = []
            for b in r:
                row_btns.append(
                    InlineKeyboardButton(text=b.get("text", "Button"), url=b.get("url"))
                )
            user_rows.append(row_btns)
        kb = InlineKeyboardMarkup(
            inline_keyboard=user_rows + (base_kb.inline_keyboard or [])
        )
    prev_id = data.get("preview_msg_id")
    if prev_id:
        await _safe_edit_reply_markup(tg_bot, chat_id, prev_id, kb)


## Перенесено в routers/post_editor.py: cb_post_toggle_notify/autosign/pin/comments
# --- Обработчики кнопки ИИ в редакторе постов ---


@router.callback_query(F.data == CB.POST_AI)
async def cb_post_ai(callback: CallbackQuery, state: FSMContext):
    """Открыть меню ИИ для генерации/улучшения контента поста."""
    data = await state.get_data()
    data.get("channel_id", 0)
    # Показать меню ИИ в том же сообщении предпросмотра (HTML)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Генерация текста", callback_data="ai_gen_topic"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Генерация из ссылки", callback_data="ai_gen_link"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Улучшить текст", callback_data="ai_improve"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="ai_back_to_preview")],
        ]
    )
    with suppress(TelegramBadRequest):
        pid = (await state.get_data()).get(
            "preview_msg_id"
        ) or callback.message.message_id
        await tg_bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=int(pid),
            text="🤖 <b>ИИ</b>\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data == "ai_back_to_preview")
async def cb_ai_back_to_preview(callback: CallbackQuery, state: FSMContext):
    """Вернуться из меню ИИ обратно к предпросмотру."""
    data = await state.get_data()
    # Восстановим «Редактор поста» и клавиатуру в том же сообщении
    prev_id = data.get("preview_msg_id") or callback.message.message_id
    instr_html = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
    kb = await _build_preview_kb(data)
    with suppress(TelegramBadRequest):
        await tg_bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=int(prev_id),
            text=instr_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data == "ai_gen_topic")
async def cb_ai_gen_topic(callback: CallbackQuery, state: FSMContext):
    """Генерация текста по теме."""
    # Переходим в режим ввода темы
    await state.set_state(PostFSM.ai_topic_input)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ai_back_to_preview")]
        ]
    )

    text = """📝 **Генерация текста по теме**

Введите тему для генерации поста.

**Примеры:**
• Новая акция на товары со скидкой 50%
• Обзор последних новостей в IT
• Как улучшить продуктивность
• Розыгрыш призов среди подписчиков"""

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "ai_gen_link")
async def cb_ai_gen_link(callback: CallbackQuery, state: FSMContext):
    """Генерация из ссылки."""
    await state.set_state(PostFSM.ai_link_input)
    # Текущий режим (summary|rewrite|paraphrase)
    data = await state.get_data()
    mode = (data.get("ai_link_mode") or "summary").lower().strip()
    mode_label = {
        "summary": "Summary",
        "rewrite": "Rewrite",
        "paraphrase": "Paraphrase",
    }.get(mode, "Summary")
    # Сохраним режим по умолчанию, если ещё не был
    await state.update_data(ai_link_mode=mode)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Режим: {mode_label}", callback_data="ai_link_mode"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="ai_back_to_preview")],
        ]
    )

    text = """🔗 Генерация из ссылки

Отправьте ссылку на статью. Режим определяет тип обработки: Summary / Rewrite / Paraphrase.

Поддерживаются:
• Новостные сайты
• Блоги
• Статьи (Medium, VC.ru и др.)

Пример: https://vc.ru/marketing/123456-article"""

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "ai_link_mode")
async def cb_ai_link_mode(callback: CallbackQuery, state: FSMContext):
    """Переключить режим обработки ссылки: summary → rewrite → paraphrase."""
    data = await state.get_data()
    cur = (data.get("ai_link_mode") or "summary").lower().strip()
    next_map = {"summary": "rewrite", "rewrite": "paraphrase", "paraphrase": "summary"}
    new_mode = next_map.get(cur, "summary")
    await state.update_data(ai_link_mode=new_mode)
    label = {
        "summary": "Summary",
        "rewrite": "Rewrite",
        "paraphrase": "Paraphrase",
    }.get(new_mode, "Summary")
    await _safe_edit_reply_markup(
        tg_bot,
        callback.message.chat.id,
        callback.message.message_id,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Режим: {label}", callback_data="ai_link_mode"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена", callback_data="ai_back_to_preview"
                    )
                ],
            ]
        ),
    )
    await callback.answer(f"Режим: {label}")


@router.callback_query(F.data == "ai_improve")
async def cb_ai_improve(callback: CallbackQuery, state: FSMContext):
    """Улучшить существующий текст."""
    data = await state.get_data()
    payload = data.get("payload", {})

    # Проверяем, есть ли текст в посте
    current_text = ""
    if payload.get("type") == "text":
        current_text = payload.get("text", "")
    elif payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "album",
    }:
        current_text = payload.get("caption", "")

    if not current_text:
        await callback.answer("❌ Сначала добавьте текст в пост", show_alert=True)
        return

    # Показываем меню с вариантами улучшения
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Улучшить общий стиль", callback_data="ai_improve_style"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📏 Укоротить", callback_data="ai_improve_shorten"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📖 Удлинить", callback_data="ai_improve_lengthen"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎨 Изменить тон", callback_data="ai_improve_tone_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    text="😊 Добавить эмодзи", callback_data="ai_improve_emoji"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="ai_back_to_preview")],
        ]
    )

    try:
        await callback.message.edit_text(
            "✨ **Улучшение текста**\n\nВыберите, что сделать с текстом:",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


# --- Обработчик ввода темы для генерации ---
@router.message(PostFSM.ai_topic_input)
async def handle_ai_topic_input(message: Message, state: FSMContext):
    """Обработка ввода темы для генерации."""
    topic = message.text.strip()

    if not topic:
        await message.answer("❌ Тема не может быть пустой. Попробуйте ещё раз.")
        return

    # Получаем данные из состояния
    data = await state.get_data()
    channel_id = data.get("channel_id", 0)
    ai_menu_msg_id = data.get("ai_menu_msg_id")

    # Удаляем сообщение пользователя
    with suppress(TelegramBadRequest):
        await message.delete()

    # Показываем индикатор генерации
    status_msg = await message.answer("⏳ Генерирую текст...")

    # Генерируем текст
    async with AsyncSessionLocal() as session:
        from app.services.ai_generation import AIGenerationService
        from app.repositories.ai_settings import ChannelAISettingsRepo

        # Гарантируем, что ИИ включен для канала при попытке генерации
        try:
            repo = ChannelAISettingsRepo(session)
            await repo.update_enabled(channel_id, True)
        except Exception:
            pass
        service = AIGenerationService(session)

        # Собираем контекст
        context = {
            "brand": data.get("brand", ""),
            "audience": data.get("audience", "подписчики канала"),
            "cta": data.get("cta", ""),
        }

        result = await service.run_pipeline(
            channel_id=channel_id,
            mode="from_scratch",
            topic=topic,
            extra=context,
            user_id=message.from_user.id,
            prompt_key="ai_text",
        )

    # Удаляем статус
    with suppress(TelegramBadRequest):
        await status_msg.delete()
    if not result["success"]:
        err = (result.get("error") or "").lower()
        # Апселл при лимитах токенов
        if ("лимит токенов" in err) or ("limit" in err and "token" in err):
            kb = await _build_pro_upsell_kb(message, channel_id)
            with suppress(TelegramBadRequest):
                await message.answer(
                    "Превышен лимит токенов. В Pro — больше лимиты и быстрый доступ к ИИ.",
                    reply_markup=kb,
                )
            await _log_ai_limit_to_admin(message, channel_id)
        else:
            error_text = (
                f"❌ Ошибка генерации:\n{result.get('error', 'Неизвестная ошибка')}"
            )
            await message.answer(error_text)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="← Назад", callback_data="ai_back_to_preview"
                        )
                    ]
                ]
            )
            if ai_menu_msg_id:
                with suppress(TelegramBadRequest):
                    await tg_bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=ai_menu_msg_id,
                        text=error_text,
                        reply_markup=kb,
                    )
        await state.clear()
        return

    # Успешная генерация - обновляем payload поста
    generated_text = result["text"]
    tokens_used = result.get("tokens_used", 0)

    # Обновляем payload с новым текстом
    payload = data.get("payload", {})
    if not payload:
        # Создаём новый payload если его не было
        payload = {"type": "text", "text": generated_text}
    else:
        # Обновляем текст или подпись
        if payload.get("type") == "text":
            payload["text"] = generated_text
        elif payload.get("type") in {
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "album",
        }:
            payload["caption"] = generated_text
        else:
            payload["text"] = generated_text
            payload["type"] = "text"

    await state.update_data(payload=payload)

    # Очищаем состояние ИИ
    await state.set_state(PostFSM.preview)

    # Удаляем меню ИИ
    if ai_menu_msg_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=message.chat.id, message_id=ai_menu_msg_id
            )

    # Показываем новый предпросмотр с сгенерированным текстом
    notify_on = bool(data.get("notify_on", True))
    autosign_on = bool(data.get("autosign_on", False))
    pin_on = bool(data.get("pin_on", False))
    comments_on = bool(data.get("comments_on", True))
    is_draft = bool(data.get("is_draft", False))
    edit_mode = bool(data.get("edit_mode", False))

    preview = await _send_preview_message(
        message,
        payload,
        notify_on=notify_on,
        autosign_on=autosign_on,
        pin_on=pin_on,
        comments_on=comments_on,
        is_draft=is_draft,
        edit_mode=edit_mode,
        has_buttons=bool(payload.get("buttons")),
        state=state,
    )

    await state.update_data(preview_msg_id=preview.message_id)

    # Кнопка «Применить ответ →» для быстрого возврата в редактор уже совершена вставкой выше,
    # поэтому просто дадим кнопку возврата к карточке создания
    kb_back = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Применить ответ →", callback_data="ai_back_to_preview"
                )
            ]
        ]
    )
    success_text = f"✅ Текст сгенерирован!\nИспользовано токенов: {tokens_used}"
    await message.answer(success_text, reply_markup=kb_back)


# --- Обработчик ввода ссылки для генерации ---


@router.message(PostFSM.ai_link_input)
async def handle_ai_link_input(message: Message, state: FSMContext):
    """Обработка ввода ссылки для саммари."""
    url = message.text.strip()

    # Проверка, что это похоже на URL
    if not url.startswith("http://") and not url.startswith("https://"):
        await message.answer("❌ Это не похоже на ссылку. Попробуйте ещё раз.")
        return

    with suppress(TelegramBadRequest):
        await message.delete()

    data = await state.get_data()
    channel_id = data.get("channel_id", 0)

    status_msg = await message.answer("⏳ Загружаю статью и генерирую текст...")

    try:
        async with AsyncSessionLocal() as session:
            service = AIGenerationService(session)
            mode = (data.get("ai_link_mode") or "summary").lower().strip()
            if mode not in {"summary", "rewrite", "paraphrase"}:
                mode = "summary"
            result = await service.run_pipeline(
                channel_id=channel_id,
                mode="from_link",
                url=url,
                extra={"link_mode": mode},
                user_id=message.from_user.id,
                prompt_key="ai_link",
            )
            with suppress(TelegramBadRequest):
                await status_msg.delete()
        if not result["success"]:
            err = (result.get("error") or "").lower()
            if ("лимит токенов" in err) or ("limit" in err and "token" in err):
                kb = await _build_pro_upsell_kb(message, channel_id)
                with suppress(TelegramBadRequest):
                    await message.answer(
                        "Превышен лимит токенов. В Pro — больше лимиты и быстрый доступ к ИИ.",
                        reply_markup=kb,
                    )
                await _log_ai_limit_to_admin(message, channel_id)
            else:
                await message.answer(
                    f"❌ Ошибка:\n{result.get('error', 'Неизвестная ошибка')}"
                )
            await state.set_state(PostFSM.preview)
            return

        # Обновляем payload
        generated_text = result["text"]
        tokens_used = result.get("tokens_used", 0)

        payload = data.get("payload", {})
        if payload.get("type") in {
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "album",
        }:
            payload["caption"] = generated_text
        else:
            payload["type"] = "text"
            payload["text"] = generated_text

        await state.update_data(payload=payload)
        await state.set_state(PostFSM.preview)

        # Показываем предпросмотр
        notify_on = bool(data.get("notify_on", True))
        autosign_on = bool(data.get("autosign_on", False))
        pin_on = bool(data.get("pin_on", False))
        comments_on = bool(data.get("comments_on", True))
        is_draft = bool(data.get("is_draft", False))
        edit_mode = bool(data.get("edit_mode", False))

        preview = await _send_preview_message(
            message,
            payload,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=is_draft,
            edit_mode=edit_mode,
            has_buttons=bool(payload.get("buttons")),
            state=state,
        )

        await state.update_data(preview_msg_id=preview.message_id)
        mode_done = {
            "summary": "Саммари",
            "rewrite": "Рерайт",
            "paraphrase": "Перефраз",
        }.get(mode, "Саммари")
        await message.answer(
            f"✅ {mode_done} готов!\nИспользовано токенов: {tokens_used}"
        )

    except Exception as e:
        logger.error(f"Ошибка при генерации из ссылки: {e}")
        await status_msg.edit_text(f"❌ Произошла ошибка: {str(e)}")
        await state.set_state(PostFSM.preview)


# --- Обработчики улучшения текста ---


@router.callback_query(F.data.startswith("ai_improve_"))
async def cb_ai_improve_action(callback: CallbackQuery, state: FSMContext):
    """Выполнение конкретного улучшения текста."""
    action = callback.data.split("_")[-1]

    instructions = {
        "style": "улучши общий стиль текста, сделай его более привлекательным и читабельным",
        "shorten": "укороти этот текст, оставив только главное",
        "lengthen": "расширь этот текст, добавив больше деталей и примеров",
        "emoji": "добавь эмодзи в текст, чтобы сделать его более живым",
    }

    # Для "tone_menu" показываем подменю
    if action == "tone":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="😊 Дружелюбный", callback_data="ai_improve_tone_friendly"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🎓 Экспертный", callback_data="ai_improve_tone_expert"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 Официальный", callback_data="ai_improve_tone_official"
                    )
                ],
                [InlineKeyboardButton(text="← Назад", callback_data="ai_improve")],
            ]
        )
        try:
            await callback.message.edit_text(
                "🎨 Выберите желаемый тон:", reply_markup=kb
            )
        except TelegramBadRequest:
            pass
        return await callback.answer()

    # Если это выбор тона
    if action.startswith("tone_"):
        tone = action.replace("tone_", "")
        tone_labels = {
            "friendly": "дружелюбным",
            "expert": "экспертным",
            "official": "официальным",
        }
        instructions[action] = (
            f"перепиши этот текст в {tone_labels.get(tone, tone)} тоне"
        )

    instruction = instructions.get(action, "улучши текст")

    data = await state.get_data()
    channel_id = data.get("channel_id", 0)
    payload = data.get("payload", {})

    # Извлекаем текущий текст
    current_text = ""
    if payload.get("type") == "text":
        current_text = payload.get("text", "")
    elif payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "album",
    }:
        current_text = payload.get("caption", "")

    if not current_text:
        await callback.answer("❌ Нет текста для улучшения", show_alert=True)
        return

    # Удаляем меню
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    status_msg = await callback.message.answer("⏳ Улучшаю текст...")

    try:
        async with AsyncSessionLocal() as session:
            service = AIGenerationService(session)
            result = await service.run_pipeline(
                channel_id=channel_id,
                mode="improve",
                original_text=current_text,
                instruction=instruction,
                user_id=callback.from_user.id,
                prompt_key="ai_improve",
            )
            with suppress(TelegramBadRequest):
                await status_msg.delete()

        if not result["success"]:
            await callback.message.answer(
                f"❌ Ошибка:\n{result.get('error', 'Неизвестная ошибка')}"
            )
            await state.set_state(PostFSM.preview)
            return

        # Обновляем payload
        improved_text = result["text"]
        tokens_used = result.get("tokens_used", 0)

        if payload.get("type") == "text":
            payload["text"] = improved_text
        else:
            payload["caption"] = improved_text

        await state.update_data(payload=payload)
        await state.set_state(PostFSM.preview)

        # Показываем предпросмотр
        notify_on = bool(data.get("notify_on", True))
        autosign_on = bool(data.get("autosign_on", False))
        pin_on = bool(data.get("pin_on", False))
        comments_on = bool(data.get("comments_on", True))
        is_draft = bool(data.get("is_draft", False))
        edit_mode = bool(data.get("edit_mode", False))

        preview = await _send_preview_message(
            callback.message,
            payload,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=is_draft,
            edit_mode=edit_mode,
            has_buttons=bool(payload.get("buttons")),
            state=state,
        )

        await state.update_data(preview_msg_id=preview.message_id)
        await callback.message.answer(
            f"✅ Текст улучшен!\nИспользовано токенов: {tokens_used}"
        )

    except Exception as e:
        logger.error(f"Ошибка при улучшении текста: {e}")
        await status_msg.edit_text(f"❌ Произошла ошибка: {str(e)}")
        await state.set_state(PostFSM.preview)


# Заглушка для пока не реализованных кнопок редактора
## Перенесено в routers/post_editor.py: cb_post_add_button


## Перенесено в routers/post_editor.py: cb_post_edit_text


@router.message(PostFSM.caption)
async def on_post_edit_text_input(message: Message, state: FSMContext):
    # Глобальные кнопки reply разрешены в любых режимах
    if await _try_handle_global_reply(message, state):
        return
    data = await state.get_data()
    payload = _payload_from(data)
    # Если ожидаем ввод цены за пост — обработаем отдельно
    st = await state.get_data()
    if st.get("_awaiting_paid_price"):
        txt = (message.text or "").strip()
        try:
            val = int(txt)
        except Exception:
            return await message.answer("❌ Введите целое число от 5 до 2500")
        if val < 5 or val > 2500:
            return await message.answer("❌ Диапазон: 5–2500 звёзд")
        payload = _payload_from(st)
        payload["media_paid_price"] = int(val)
        payload["media_paid_on"] = True
        await state.update_data(payload=payload, _awaiting_paid_price=False)
        # Пересоздадим предпросмотр и карточку: сначала удалим старую карточку, потом отправим новое медиа и новую карточку
        # Удалим старый предпросмотр и альбом, если был
        state_after = await state.get_data()
        prev_id = state_after.get("preview_msg_id") or getattr(
            message, "message_id", None
        )
        media_id = state_after.get("preview_media_id")
        if prev_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=message.chat.id, message_id=int(prev_id)
                )
        if media_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=message.chat.id, message_id=int(media_id)
                )
        for mid in state_after.get("preview_album_ids") or []:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=message.chat.id, message_id=int(mid)
                )
        # Очистим ссылки на предыдущие превью в состоянии перед отправкой нового
        await state.update_data(
            preview_media_id=None, preview_msg_id=None, preview_album_ids=[]
        )
        # 1) Отправим платное медиа заново с новой ценой
        paid_msg_id = None
        try:
            from aiogram.types import InputPaidMediaPhoto, InputPaidMediaVideo
        except Exception:
            InputPaidMediaPhoto = None  # type: ignore
            InputPaidMediaVideo = None  # type: ignore
        ptype = payload.get("type")
        if ptype == "photo" and InputPaidMediaPhoto is not None:
            try:
                pm = await tg_bot.send_paid_media(
                    chat_id=message.chat.id,
                    star_count=int(payload.get("media_paid_price") or 0),
                    media=[InputPaidMediaPhoto(media=payload.get("file_id"))],
                    caption=(payload.get("caption") or None),
                    parse_mode=(
                        None if payload.get("caption_entities") else "Markdown"
                    ),
                    caption_entities=payload.get("caption_entities"),
                )
                paid_msg_id = getattr(pm, "message_id", None)
            except Exception:
                m = await message.answer_photo(
                    payload.get("file_id"),
                    caption=(payload.get("caption") or None),
                    parse_mode=(
                        None if payload.get("caption_entities") else "Markdown"
                    ),
                    caption_entities=payload.get("caption_entities"),
                )
                paid_msg_id = getattr(m, "message_id", None)
        elif ptype == "video" and InputPaidMediaVideo is not None:
            try:
                pm = await tg_bot.send_paid_media(
                    chat_id=message.chat.id,
                    star_count=int(payload.get("media_paid_price") or 0),
                    media=[InputPaidMediaVideo(media=payload.get("file_id"))],
                    caption=(payload.get("caption") or None),
                    parse_mode=(
                        None if payload.get("caption_entities") else "Markdown"
                    ),
                    caption_entities=payload.get("caption_entities"),
                )
                paid_msg_id = getattr(pm, "message_id", None)
            except Exception:
                m = await message.answer_video(
                    payload.get("file_id"),
                    caption=(payload.get("caption") or None),
                    parse_mode=(
                        None if payload.get("caption_entities") else "Markdown"
                    ),
                    caption_entities=payload.get("caption_entities"),
                )
                paid_msg_id = getattr(m, "message_id", None)
        elif ptype == "album":
            # Соберём платную медиагруппу из photo/video элементов
            items = list(payload.get("items") or [])
            paid_media = []
            for it in items:
                if it.get("type") == "photo" and InputPaidMediaPhoto is not None:
                    paid_media.append(InputPaidMediaPhoto(media=it.get("file_id")))
                elif it.get("type") == "video" and InputPaidMediaVideo is not None:
                    paid_media.append(InputPaidMediaVideo(media=it.get("file_id")))
            cap_to_use = None
            cent_to_use = None
            for it2 in items:
                if (it2.get("caption") or "").strip():
                    cap_to_use = it2.get("caption")
                    cent_to_use = it2.get("caption_entities")
            if paid_media:
                try:
                    pm = await tg_bot.send_paid_media(
                        chat_id=message.chat.id,
                        star_count=int(payload.get("media_paid_price") or 0),
                        media=paid_media,
                        caption=cap_to_use,
                        parse_mode=(None if cent_to_use else "Markdown"),
                        caption_entities=cent_to_use,
                    )
                    paid_msg_id = getattr(pm, "message_id", None)
                except Exception:
                    res = await tg_bot.send_media_group(
                        chat_id=message.chat.id,
                        media=[
                            InputMediaPhoto(media=it.get("file_id"))
                            if it.get("type") == "photo"
                            else InputMediaVideo(media=it.get("file_id"))
                            for it in items
                            if it.get("type") in {"photo", "video"}
                        ],
                    )
                    paid_msg_id = getattr((res or [None])[-1], "message_id", None)
        # Сохраним id платного превью, если отправили
        if paid_msg_id:
            await state.update_data(preview_media_id=int(paid_msg_id))
        # 2) Отправим ниже карточку «Медиа» и сохраним ее id как preview_msg_id
        from app.bot.keyboards.builders import build_media_menu_kb

        pos = payload.get("media_pos", "top")
        spoiler = bool(payload.get("media_spoiler", False))
        paid_on = bool(payload.get("media_paid_on", False))
        price = int(payload.get("media_paid_price") or 0)
        is_album = payload.get("type") == "album"
        kb = build_media_menu_kb(
            pos, spoiler, paid_on=paid_on, is_album=is_album, price=(price or None)
        )
        header_html = (
            "<b>Медиа</b>\n\nНастройте, как будет выглядеть медиа-файл в посте."
        )
        m_card = await message.answer(
            header_html,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await state.update_data(preview_msg_id=m_card.message_id)
        # Переключимся обратно в подменю «Медиа» и состояние предпросмотра, оставаясь на новой карточке
        with suppress(Exception):
            await state.update_data(ui_submenu="media")
        with suppress(Exception):
            await state.set_state(PostFSM.preview)
        return

    new_text = (message.text or message.caption or "").strip()
    if not new_text:
        return await message.answer("❌ Пустой текст")
    # Обновим payload
    if payload.get("type") == "text":
        payload["text"] = new_text
        # Сохраним entities для корректного предпросмотра без parse_mode
        if getattr(message, "entities", None):
            payload["entities"] = [e.model_dump() for e in (message.entities or [])]
        else:
            payload.pop("entities", None)
    else:
        payload["caption"] = new_text
        # Сохраним caption_entities для корректного предпросмотра без parse_mode
        if getattr(message, "caption_entities", None):
            payload["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
        elif getattr(message, "entities", None):
            # На случай, если клиент прислал текст с entities как обычное сообщение
            payload["caption_entities"] = [
                e.model_dump() for e in (message.entities or [])
            ]
        else:
            payload.pop("caption_entities", None)
    await state.update_data(payload=payload)
    # Обновим предпросмотр без прыжка: редактируем сам текст/подпись
    notify_on = bool(data.get("notify_on", True))
    autosign_on = bool(data.get("autosign_on", False))
    pin_on = bool(data.get("pin_on", False))
    comments_on = bool(data.get("comments_on", True))
    kb = post_actions(
        for_video_note=(payload.get("type") == "video_note"),
        has_media=payload.get("type")
        in {"photo", "video", "animation", "audio", "voice", "video_note", "album"},
        notify_on=notify_on,
        autosign_on=autosign_on,
        pin_on=pin_on,
        comments_on=comments_on,
        is_draft=bool(data.get("is_draft", False)),
        ai_enabled=_is_ai_enabled(data),
    )
    prev_id = data.get("preview_msg_id")
    if prev_id:
        # По требованию: удалим старый предпросмотр и отправим новый с обновлённым текстом
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(chat_id=message.chat.id, message_id=prev_id)
        notify_on = bool(data.get("notify_on", True))
        autosign_on = bool(data.get("autosign_on", False))
        pin_on = bool(data.get("pin_on", False))
        comments_on = bool(data.get("comments_on", True))
        new_prev = await _send_preview_message(
            message,
            payload,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=bool(data.get("is_draft", False)),
            edit_mode=(
                data.get("edit_chat_id") is not None
                and data.get("edit_msg_id") is not None
                and not bool(data.get("is_draft", False))
            ),
            has_buttons=False,
            state=state,
        )
        await state.update_data(preview_msg_id=new_prev.message_id)
    # Возвращаемся в предпросмотр
    await state.set_state(PostFSM.preview)
    # Удалим приглашение ввода, если есть
    prompt_id = data.get("edit_prompt_id")
    if prompt_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(chat_id=message.chat.id, message_id=prompt_id)


@router.message(PostFSM.buttons)
async def on_post_buttons_input(message: Message, state: FSMContext):
    # Глобальные команды из реплая поддерживаются
    if await _try_handle_global_reply(message, state):
        return
    text = (message.text or message.html_text or "").strip()
    if not text:
        return await message.answer("❌ Пришлите текст с кнопками по инструкции")
    # Разбор формата в список списков кнопок
    rows: list[list[dict]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.split("|")]
        row: list[dict] = []
        for cell in cells:
            # Ожидаем "Название - ссылка"
            parts = [p.strip() for p in cell.split("-", 1)]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return await message.answer(
                    "❌ Неверный формат. Используйте: Название - ссылка"
                )
            row.append({"text": parts[0], "url": parts[1]})
        if row:
            rows.append(row)
    if not rows:
        return await message.answer("❌ Не удалось распарсить кнопки")
    # Сохраняем в payload
    data = await state.get_data()
    payload = _payload_from(data)
    payload["buttons"] = rows
    await state.update_data(payload=payload)
    # Перерисовываем предпросмотр с обновлённой клавиатурой: учитываем наличие кнопок
    notify_on = bool(data.get("notify_on", True))
    autosign_on = bool(data.get("autosign_on", False))
    pin_on = bool(data.get("pin_on", False))
    comments_on = bool(data.get("comments_on", True))
    # По требованиям: удалим старый предпросмотр и отправим НОВЫЙ под сообщением пользователя,
    # чтобы редактор оказался ниже текста пользователя
    data2 = await state.get_data()
    prev_id = data2.get("preview_msg_id")
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(chat_id=message.chat.id, message_id=prev_id)
    new_prev = await _send_preview_message(
        message,
        payload,
        notify_on=notify_on,
        autosign_on=autosign_on,
        pin_on=pin_on,
        comments_on=comments_on,
        is_draft=bool(data.get("is_draft", False)),
        edit_mode=(
            data.get("edit_chat_id") is not None
            and data.get("edit_msg_id") is not None
            and not bool(data.get("is_draft", False))
        ),
        has_buttons=True,
    )
    await state.update_data(preview_msg_id=new_prev.message_id)
    # Возврат в предпросмотр и очистка возможных подсказок
    await state.set_state(PostFSM.preview)
    await state.update_data(buttons_prompt_ids=[])


## moved to post_editor: cb_post_delete_all_buttons
## moved to post_editor: cb_post_remove_single_button
## moved to post_editor: cb_post_add_media
## moved to post_editor: cb_post_next
## moved to post_editor: cb_post_settings_back

## moved to posting_publish: cb_post_schedule
## moved to post_editor: cb_post_back
## moved to posting_publish: cb_post_send
## moved to posting_publish: cb_post_settings_publish

## Перенесено в routers/settings.py: rp_settings
## Перенесено в routers/settings.py: cb_settings_tz_root

## Перенесено в routers/content_plan.py: rp_content_plan_entry

# Поддержка варианта с дефисом "Контент-план"
## Перенесено в routers/content_plan.py: rp_content_plan_entry_alias

## Перенесено в routers/content_plan.py: _format_date_human, _render_content_plan

## Перенесено в routers/content_plan.py: cb_cp_pick_channel

## Перенесено в routers/content_plan.py: cb_cp_day_shift

## Перенесено в routers/content_plan.py: cb_cp_day_center_nop
## Перенесено в routers/content_plan.py: cb_cp_open_post


## Перенесено в routers/content_plan.py: cb_cp_delete_post
## Перенесено в routers/content_plan.py: cb_cp_edit_post


## moved to post_editor: cb_media_menu

## moved to post_editor: cb_preview_menu

## moved to post_editor: cb_preview_toggle

## moved to post_editor: cb_preview_pos_toggle

## moved to post_editor: cb_preview_clear
## moved to post_editor: cb_media_pos_toggle
## moved to post_editor: cb_media_spoiler_toggle


## moved to post_editor: cb_media_replace


## moved to routers/utils/time_utils.py: _month_name_ru, _build_calendar_kb
async def _render_calendar(
    callback: CallbackQuery,
    channel_id: int,
    focus_date: datetime,
    selected_date: datetime | None,
) -> None:
    # Оставляем текст как в сводке
    # Получим количество постов за выбранную дату
    from app.domain.models import PostTask, Channel

    async with AsyncSessionLocal() as session:
        start = datetime(
            (selected_date or focus_date).year,
            (selected_date or focus_date).month,
            (selected_date or focus_date).day,
            0,
            0,
            0,
            tzinfo=timezone.utc,
        )
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
        res = await session.execute(
            select(func.count())
            .select_from(PostTask)
            .where(
                (PostTask.channel_id == channel_id)
                & (PostTask.status == "pending")
                & (PostTask.scheduled_at >= start)
                & (PostTask.scheduled_at <= end)
            )
        )
        count = int(res.scalar() or 0)
        ch = await session.get(Channel, channel_id)
        ch_title = ch.title or str(ch.tg_chat_id)
    try:
        chat_info = await tg_bot.get_chat(ch.tg_chat_id)
        uname = getattr(chat_info, "username", None)
        if uname:
            title_link = f'<a href="https://t.me/{html.escape(uname)}">{html.escape(ch_title)}</a>'
        else:
            title_link = html.escape(ch_title)
    except Exception:
        title_link = html.escape(ch_title)
    months = [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]
    cur = selected_date or focus_date
    text = f"На {cur.day} {months[cur.month - 1]} {cur.year} в канале {title_link} запланировано {count} постов."
    kb = _build_calendar_kb(channel_id, focus_date, selected_date or focus_date)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


## Перенесено в routers/content_plan.py: cb_cp_open_calendar

## Перенесено в routers/content_plan.py: cb_cp_calendar_back

## Перенесено в routers/content_plan.py: cb_cp_month_shift

## Перенесено в routers/content_plan.py: cb_cp_pick_day


## Перенесено в routers/content_plan.py: cb_cp_back_channels
@router.message(F.text == "Отложенные посты")
async def rp_scheduled_posts(message: Message):
    # Заглушка: простой ответ, чтобы кнопка не была пустой
    await message.answer("Скоро здесь появится список запланированных постов")


@router.callback_query(F.data == "settings_channels_list")
async def cb_settings_channels_list(callback: CallbackQuery):
    # moved to settings.py
    from app.bot.routers.settings import cb_settings_channels_list as _cb

    return await _cb(callback)


@router.callback_query(F.data.startswith("settings_neuropost_"))
async def cb_settings_neuropost(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Текст", callback_data=f"neu_text_{cid}"),
                InlineKeyboardButton(text="Медиа", callback_data=f"neu_media_{cid}"),
            ],
            [
                InlineKeyboardButton(
                    text="Хештеги/CTA", callback_data=f"neu_tags_{cid}"
                ),
                InlineKeyboardButton(
                    text="Планировщик", callback_data=f"neu_schedule_{cid}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Источники", callback_data=f"neu_sources_{cid}"
                ),
                InlineKeyboardButton(
                    text="Модерация", callback_data=f"neu_moder_{cid}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Тест/черновик", callback_data=f"neu_test_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"channels_settings_{cid}"
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text("🤖 ИИ\nВыберите раздел:", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("neu_text_"))
async def cb_neu_text(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    # Меню настроек текста ИИ
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выбрать стиль", callback_data=f"ai_choose_preset_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Пользовательские промпты", callback_data=f"ai_csp_list_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Приоритет генерации", callback_data=f"ai_priority_{cid}"
                )
            ],
            [
                InlineKeyboardButton(text="Тон", callback_data=f"ai_tone_{cid}"),
                InlineKeyboardButton(text="Длина", callback_data=f"ai_length_{cid}"),
            ],
            # Удалено: кнопка языка
            [
                InlineKeyboardButton(
                    text="История диалога", callback_data=f"ai_text_history_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text(
            "📝 Настройки генерации текста", reply_markup=kb
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("neu_media_"))
async def cb_neu_media(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ]
        ]
    )
    try:
        await callback.message.edit_text(
            "🎞 Медиа\nРаздел в разработке.", reply_markup=kb
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("neu_tags_"))
async def cb_neu_tags(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    # Меню настроек хештегов и CTA
    # Загрузим текущие значения из БД
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
    hashtags_status = "✅ Вкл" if st.hashtags_enabled else "❌ Выкл"
    cta_status = "✅ Вкл" if st.cta_enabled else "❌ Выкл"
    text = f"#️⃣ Хештеги/CTA\n\nХештеги: {hashtags_status}\nКоличество: {st.hashtags_count}\nCTA: {cta_status}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ Хештеги" if st.hashtags_enabled else "☑️ Хештеги"),
                    callback_data=f"ai_toggle_hashtags_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Количество хештегов", callback_data=f"ai_hashtags_count_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ CTA" if st.cta_enabled else "☑️ CTA"),
                    callback_data=f"ai_toggle_cta_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("neu_schedule_"))
async def cb_neu_schedule(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ]
        ]
    )
    try:
        await callback.message.edit_text(
            "🗓 Планировщик\nРаздел в разработке.", reply_markup=kb
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("neu_sources_"))
async def cb_neu_sources(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    # Меню источников
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить источник", callback_data=f"ai_source_add_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Список источников", callback_data=f"ai_source_list_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text(
            "🔁 Источники\n\nДобавляйте URL, RSS или @каналы для рерайта/саммари.",
            reply_markup=kb,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    except TelegramNetworkError:
        # Если сеть отвалилась при edit_text — отправим новое сообщение вместо редактирования
        with suppress(Exception):
            await callback.message.answer(
                "🔁 Источники\n\nДобавляйте URL, RSS или @каналы для рерайта/саммари.",
                reply_markup=kb,
            )
    await callback.answer()


## Перенесено в routers/sources.py: cb_ai_source_add
## Перенесено в routers/sources.py: handle_source_input
## Перенесено в routers/sources.py: cb_ai_source_list_from_message


## Перенесено в routers/sources.py: cb_ai_source_list
## Перенесено в routers/sources.py: _edit_ai_sources_list
## Перенесено в routers/sources.py: cb_ai_source_toggle


## Перенесено в routers/sources.py: cb_ai_source_cite
## Перенесено в routers/sources.py: cb_ai_source_mode


## Перенесено в routers/sources.py: cb_ai_source_delete


@router.callback_query(F.data.startswith("neu_moder_"))
async def cb_neu_moder(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    # Меню модерации
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
    moderation_status = "✅ Вкл" if st.moderation_enabled else "❌ Выкл"
    links_status = "✅ Разрешены" if st.links_allowed else "❌ Запрещены"
    utm_status = "✅ Вкл" if st.utm_enabled else "❌ Выкл"
    forbidden_count = len(st.forbidden_words or [])
    text = f"🛡 Модерация\n\nМодерация: {moderation_status}\nСсылки: {links_status}\nUTM: {utm_status}\nЗапрещённых слов: {forbidden_count}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ Модерация" if st.moderation_enabled else "☑️ Модерация"),
                    callback_data=f"ai_toggle_moderation_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Запрещённые слова ({forbidden_count})",
                    callback_data=f"ai_forbidden_words_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ Ссылки" if st.links_allowed else "☑️ Ссылки"),
                    callback_data=f"ai_toggle_links_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ UTM" if st.utm_enabled else "☑️ UTM"),
                    callback_data=f"ai_toggle_utm_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


## Перенесено в routers/moderation.py: cb_ai_toggle_moderation
async def cb_ai_toggle_moderation(callback: CallbackQuery):
    """Переключить модерацию."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        await repo.update_params(cid, moderation_enabled=not st.moderation_enabled)

    await callback.answer(
        "✅ Модерация " + ("включена" if not st.moderation_enabled else "выключена")
    )
    # Обновляем меню модерации без изменения callback.data
    await cb_neu_moder(callback)


## Перенесено в routers/moderation.py: cb_ai_toggle_links
async def cb_ai_toggle_links(callback: CallbackQuery):
    """Переключить разрешение ссылок."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        await repo.update_params(cid, links_allowed=not st.links_allowed)

    await callback.answer(
        "✅ Ссылки " + ("разрешены" if not st.links_allowed else "запрещены")
    )
    await cb_neu_moder(callback)


## Перенесено в routers/moderation.py: cb_ai_toggle_utm
async def cb_ai_toggle_utm(callback: CallbackQuery):
    """Переключить UTM-метки."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        await repo.update_params(cid, utm_enabled=not st.utm_enabled)

    await callback.answer(
        "✅ UTM-метки " + ("включены" if not st.utm_enabled else "выключены")
    )
    await cb_neu_moder(callback)


## Перенесено в routers/moderation.py: cb_ai_forbidden_words
async def cb_ai_forbidden_words(callback: CallbackQuery, state: FSMContext):
    """Управление запрещёнными словами."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)

    words = st.forbidden_words or []
    words_list = ", ".join(words) if words else "нет"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить слова", callback_data=f"ai_forbidden_add_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Очистить список", callback_data=f"ai_forbidden_clear_{cid}"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"neu_moder_{cid}")],
        ]
    )

    text = f"📝 **Запрещённые слова**\n\nТекущий список:\n{words_list}\n\nДобавьте слова, которые не должны появляться в сгенерированных постах."

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


## Перенесено в routers/moderation.py: cb_ai_forbidden_add
async def cb_ai_forbidden_add(callback: CallbackQuery, state: FSMContext):
    """Добавление запрещённых слов."""
    cid = int(callback.data.split("_")[-1])

    await state.set_state(SettingsFSM.forbidden_input)
    await state.update_data(forbidden_channel_id=cid)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"ai_forbidden_words_{cid}"
                )
            ]
        ]
    )

    text = """📝 **Добавление запрещённых слов**
Отправьте список слов через запятую или пробел.

Пример:
`казино, ставки, азартные игры`

Или:
`блокировка бан удалён`"""

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


## Перенесено в routers/moderation.py: handle_forbidden_input
async def handle_forbidden_input(message: Message, state: FSMContext):
    """Обработка ввода запрещённых слов."""
    data = await state.get_data()
    cid = data.get("forbidden_channel_id")

    if not cid:
        await message.answer("❌ Ошибка: канал не найден")
        await state.clear()
        return

    text_input = message.text.strip()

    # Парсим слова (разделитель: запятая, пробел, точка с запятой)
    import re

    words = re.split(r"[,;\s]+", text_input)
    words = [w.strip().lower() for w in words if w.strip()]

    if not words:
        await message.answer("❌ Не распознаны слова. Попробуйте ещё раз.")
        return

    # Добавляем к существующим
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)

        existing = list(st.forbidden_words or [])
        # Объединяем и убираем дубли
        all_words = list(set(existing + words))
        await repo.update_forbidden_words(cid, all_words)

    await state.clear()
    await message.answer(
        f"✅ Добавлено {len(words)} слов. Всего в списке: {len(all_words)}"
    )

    # Возвращаемся к меню запрещённых слов
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Назад", callback_data=f"ai_forbidden_words_{cid}"
                )
            ]
        ]
    )
    await message.answer("Вернуться:", reply_markup=kb)


## Перенесено в routers/moderation.py: cb_ai_forbidden_clear
async def cb_ai_forbidden_clear(callback: CallbackQuery):
    """Очистить список запрещённых слов."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_forbidden_words(cid, [])

    await callback.answer("✅ Список очищен")
    # Возвращаемся к меню запрещённых слов без изменения callback.data
    await cb_ai_forbidden_words(callback, FSMContext)


@router.callback_query(F.data.startswith("ai_choose_preset_"))
async def cb_ai_choose_preset(callback: CallbackQuery):
    """Выбор пресета."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import AIPresetsRepo

        repo = AIPresetsRepo(session)
        presets = await repo.list_active()

    if not presets:
        await callback.answer("❌ Пресеты не найдены", show_alert=True)
        return

    # Уберём эмодзи из подписей и выведем кнопки по 2 в ряд
    def _strip_emojis(text: str) -> str:
        try:
            import re

            emoji_pattern = re.compile(
                "[\U0001f1e6-\U0001f1ff]|[\U0001f300-\U0001f5ff]|[\U0001f600-\U0001f64f]|"
                "[\U0001f680-\U0001f6ff]|[\U0001f700-\U0001f77f]|[\U0001f780-\U0001f7ff]|"
                "[\U0001f800-\U0001f8ff]|[\U0001f900-\U0001f9ff]|[\U0001fa00-\U0001faff]|"
                "[\u2600-\u26ff]|[\u2700-\u27bf]|[\u2b00-\u2bff]|\u200d|\ufe0f"
            )
            return emoji_pattern.sub("", text or "").strip()
        except Exception:
            return (text or "").strip()

    kb_rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for preset in presets:
        label = _strip_emojis(getattr(preset, "title", "") or "")
        row.append(
            InlineKeyboardButton(
                text=label, callback_data=f"ai_set_preset_{cid}_{preset.id}"
            )
        )
        if len(row) == 2:
            kb_rows.append(row)
            row = []
    if row:
        kb_rows.append(row)
    # назад отдельной строкой
    kb_rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")]
    )

    text = "📋 **Выберите пресет:**\n\nКаждый пресет содержит готовый промпт для определённого типа контента."

    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("ai_set_preset_"))
async def cb_ai_set_preset(callback: CallbackQuery):
    """Установить выбранный пресет."""
    parts = callback.data.split("_")
    cid = int(parts[3])
    preset_id = int(parts[4])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo, AIPresetsRepo

        ai_repo = ChannelAISettingsRepo(session)
        presets_repo = AIPresetsRepo(session)

        await ai_repo.update_preset(cid, preset_id)
        preset = await presets_repo.get_by_id(preset_id)

    await callback.answer(
        f"✅ Установлен пресет: {preset.title if preset else 'Неизвестный'}",
        show_alert=True,
    )
    # безопасный возврат в меню текста по явному cid
    try:
        await _render_neu_text_menu(callback, cid)
    except Exception:
        # fallback на старый хендлер, если что-то пойдёт не так
        await cb_neu_text(callback)


@router.callback_query(F.data.startswith("ai_tone_"))
async def cb_ai_tone(callback: CallbackQuery):
    """Выбор тона."""
    cid = int(callback.data.split("_")[-1])

    tones = [
        ("friendly", "Дружелюбный"),
        ("expert", "Экспертный"),
        ("conversational", "Разговорный"),
        ("official", "Официальный"),
        ("storytelling", "Сторителлинг"),
        ("promo", "Промо"),
    ]

    kb_rows = []
    for tone_code, tone_label in tones:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=tone_label, callback_data=f"ai_set_tone_{cid}_{tone_code}"
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")]
    )

    text = (
        "🎨 **Выберите тон:**\n\n"
        "• Дружелюбный — лёгкий, понятный, 0–2 эмодзи\n"
        "• Экспертный — точный, без эмодзи\n"
        "• Разговорный — живой, допускает вопросы, до 3 эмодзи\n"
        "• Официальный — нейтрально-деловой, без эмодзи\n"
        "• Сторителлинг — хук → история → вывод\n"
        "• Промо — выгоды, конкретика, понятный CTA"
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("ai_set_tone_"))
async def cb_ai_set_tone(callback: CallbackQuery):
    """Установить тон."""
    parts = callback.data.split("_")
    cid = int(parts[3])
    tone = parts[4]

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_params(cid, tone=tone)

    tone_labels = {
        "friendly": "Дружелюбный",
        "expert": "Экспертный",
        "conversational": "Разговорный",
        "official": "Официальный",
        "storytelling": "Сторителлинг",
        "promo": "Промо",
    }
    await callback.answer(f"✅ Тон: {tone_labels.get(tone, tone)}")
    try:
        await _render_neu_text_menu(callback, cid)
    except Exception:
        await cb_neu_text(callback)


@router.callback_query(F.data.startswith("ai_length_"))
async def cb_ai_length(callback: CallbackQuery):
    """Выбор длины."""
    cid = int(callback.data.split("_")[-1])

    lengths = [
        ("short", "Короткий (до 500 символов)"),
        ("medium", "Средний (500-1200 символов)"),
        ("long", "Длинный (1200+ символов)"),
    ]

    kb_rows = []
    for length_code, length_label in lengths:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=length_label,
                    callback_data=f"ai_set_length_{cid}_{length_code}",
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")]
    )

    text = "📏 **Выберите длину поста:**"

    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("ai_set_length_"))
async def cb_ai_set_length(callback: CallbackQuery):
    """Установить длину."""
    parts = callback.data.split("_")
    cid = int(parts[3])
    length = parts[4]

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_params(cid, length=length)

    length_labels = {"short": "Короткий", "medium": "Средний", "long": "Длинный"}
    await callback.answer(f"✅ Длина: {length_labels.get(length, length)}")
    try:
        await _render_neu_text_menu(callback, cid)
    except Exception:
        await cb_neu_text(callback)


@router.callback_query(F.data.startswith("ai_emoji_"))
async def cb_ai_emoji(callback: CallbackQuery):
    """Выбор уровня эмодзи."""
    cid = int(callback.data.split("_")[-1])

    emoji_levels = [
        (0, "🚫 Без эмодзи"),
        (1, "😊 Умеренно"),
        (2, "😍 Много"),
        (3, "🤩 Очень много"),
    ]

    kb_rows = []
    for level, label in emoji_levels:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"ai_set_emoji_{cid}_{level}"
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")]
    )

    text = "😊 **Выберите плотность эмодзи:**"

    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("ai_set_emoji_"))
async def cb_ai_set_emoji(callback: CallbackQuery):
    """Установить уровень эмодзи."""
    parts = callback.data.split("_")
    cid = int(parts[3])
    level = int(parts[4])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_params(cid, emoji_level=level)

    await callback.answer(f"✅ Эмодзи: {level}/3")
    await cb_neu_text(callback)


@router.callback_query(F.data.startswith("ai_toggle_hashtags_"))
async def cb_ai_toggle_hashtags(callback: CallbackQuery):
    """Переключить генерацию хештегов."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        new_value = not st.hashtags_enabled
        await repo.update_params(cid, hashtags_enabled=new_value)

    await callback.answer("✅ Хештеги " + ("включены" if new_value else "выключены"))

    # Обновляем меню с актуальными данными
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st_updated = await repo.get_or_create(cid)

    hashtags_status = "✅ Вкл" if st_updated.hashtags_enabled else "❌ Выкл"
    cta_status = "✅ Вкл" if st_updated.cta_enabled else "❌ Выкл"
    text = f"#️⃣ Хештеги/CTA\n\nХештеги: {hashtags_status}\nКоличество: {st_updated.hashtags_count}\nCTA: {cta_status}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ Хештеги" if st_updated.hashtags_enabled else "☑️ Хештеги"),
                    callback_data=f"ai_toggle_hashtags_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Количество хештегов", callback_data=f"ai_hashtags_count_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ CTA" if st_updated.cta_enabled else "☑️ CTA"),
                    callback_data=f"ai_toggle_cta_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data.startswith("ai_hashtags_count_"))
async def cb_ai_hashtags_count(callback: CallbackQuery):
    """Выбор количества хештегов."""
    cid = int(callback.data.split("_")[-1])

    counts = [1, 2, 3, 5, 7]
    kb_rows = []
    for count in counts:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{count} хештегов",
                    callback_data=f"ai_set_hashtags_count_{cid}_{count}",
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"neu_tags_{cid}")]
    )

    text = "#️⃣ **Количество хештегов:**\n\nВыберите, сколько хештегов генерировать."

    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("ai_set_hashtags_count_"))
async def cb_ai_set_hashtags_count(callback: CallbackQuery):
    """Установить количество хештегов."""
    parts = callback.data.split("_")
    cid = int(parts[4])
    count = int(parts[5])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_params(cid, hashtags_count=count)

    await callback.answer(f"✅ Количество: {count}")
    await cb_neu_tags(callback)


@router.callback_query(F.data.startswith("ai_toggle_cta_"))
async def cb_ai_toggle_cta(callback: CallbackQuery):
    """Переключить CTA."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        new_value = not st.cta_enabled
        await repo.update_params(cid, cta_enabled=new_value)

    await callback.answer("✅ CTA " + ("включен" if new_value else "выключен"))

    # Обновляем меню с актуальными данными
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st_updated = await repo.get_or_create(cid)

    hashtags_status = "✅ Вкл" if st_updated.hashtags_enabled else "❌ Выкл"
    cta_status = "✅ Вкл" if st_updated.cta_enabled else "❌ Выкл"
    text = f"#️⃣ Хештеги/CTA\n\nХештеги: {hashtags_status}\nКоличество: {st_updated.hashtags_count}\nCTA: {cta_status}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ Хештеги" if st_updated.hashtags_enabled else "☑️ Хештеги"),
                    callback_data=f"ai_toggle_hashtags_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Количество хештегов", callback_data=f"ai_hashtags_count_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ CTA" if st_updated.cta_enabled else "☑️ CTA"),
                    callback_data=f"ai_toggle_cta_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def cb_ai_custom_prompt(callback: CallbackQuery):
    """Настройка пользовательского промпта."""
    cid = int(callback.data.split("_")[-1])
    # Получим текущие значения
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
    cur_sys = st.custom_prompt or ""
    cur_user = st.user_prompt_template or ""
    mode_label = (
        "Пресет"
        if getattr(st, "preset_id", None)
        else ("Свой" if cur_sys or cur_user else "Дефолт")
    )
    text = f"Пользовательский промпт\n\nПриоритет: {mode_label}\n\nВыберите действие:"
    # Кнопка приоритета должна отображать ТЕКУЩИЙ режим (не цель переключения)
    is_preset = bool(getattr(st, "preset_id", None))
    priority_label = "Приоритет: Пресет" if is_preset else "Приоритет: Пользовательский"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пользовательские промпты", callback_data=f"ai_csp_list_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=priority_label, callback_data=f"ai_custom_priority_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Выбрать пресет", callback_data=f"ai_choose_preset_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Сбросить", callback_data=f"ai_custom_reset_{cid}"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")],
        ]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ai_custom_system_"))
async def cb_ai_custom_system(callback: CallbackQuery, state: FSMContext):
    cid = int(callback.data.split("_")[-1])
    await state.set_state(SettingsFSM.custom_system_input)
    await state.update_data(custom_prompt_cid=cid)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Введите системный промпт (сообщением).\n\nПодсказка: можно использовать переменные {topic}, {tone}, {length}, {emoji_level}, {lang}, {hashtags}, {cta}, {brand}, {audience}, {channel_title}"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("ai_custom_user_"))
async def cb_ai_custom_user(callback: CallbackQuery, state: FSMContext):
    cid = int(callback.data.split("_")[-1])
    await state.set_state(SettingsFSM.custom_user_input)
    await state.update_data(custom_prompt_cid=cid)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Введите шаблон пользователя (user prompt).\n\nПример: {topic}"
        )
    await callback.answer()


@router.message(SettingsFSM.custom_system_input)
async def handle_custom_system_input(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = int(data.get("custom_prompt_cid", 0))
    if not cid:
        await state.clear()
        return await message.answer("❌ Ошибка: канал не распознан")
    async with AsyncSessionLocal() as session:
        csp_id = data.get("csp_edit_id")
        if csp_id is not None:
            # Редактирование/создание записи из списка
            from app.repositories.custom_prompts import CustomSystemPromptsRepo

            repo = CustomSystemPromptsRepo(session)
            if int(csp_id) > 0:
                await repo.update_content(int(csp_id), text)
            else:
                await repo.create(cid, text, active=False)
        else:
            # Старый путь: сохранить в ChannelAISettings
            from app.repositories.ai_settings import ChannelAISettingsRepo

            repo = ChannelAISettingsRepo(session)
            await repo.update_custom_prompt(cid, system_prompt=text, user_template=None)
    await state.clear()
    # Псевдо-всплывающее уведомление (в сообщениях нет callback-алертов)
    await message.answer("✅ Системный промпт сохранён")
    # Откроем список «Мои системные промпты»
    async with AsyncSessionLocal() as session:
        from app.repositories.custom_prompts import CustomSystemPromptsRepo

        repo = CustomSystemPromptsRepo(session)
        items = await repo.list_by_channel(cid)
    rows = []
    for p in items:
        prefix = "✅" if getattr(p, "is_active", False) else "☑️"
        title = (p.content or "").strip().replace("\n", " ")[:40] or "(пусто)"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {title}", callback_data=f"ai_csp_open_{cid}_{p.id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить промпт/шаблон", callback_data=f"ai_csp_add_{cid}"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")])
    with suppress(TelegramBadRequest):
        await message.answer(
            "📚 Мои системные промпты\n\nВыберите промпт или добавьте новый.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.message(SettingsFSM.custom_user_input)
async def handle_custom_user_input(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = int(data.get("custom_prompt_cid", 0))
    if not cid:
        await state.clear()
        return await message.answer("❌ Ошибка: канал не распознан")
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_custom_prompt(cid, system_prompt=None, user_template=text)
    await state.clear()
    # Псевдо-всплывающее уведомление
    await message.answer("✅ Шаблон пользователя сохранён")
    with suppress(Exception):
        await cb_ai_custom_prompt_from_message(message, cid)


@router.callback_query(F.data.startswith("ai_custom_reset_"))
async def cb_ai_custom_reset(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_custom_prompt(cid, system_prompt=None, user_template=None)
    await callback.answer("🗑 Сброшены свой системный промпт и шаблон")
    with suppress(Exception):
        await cb_ai_custom_prompt(callback)


@router.callback_query(F.data.startswith("ai_csp_list_"))
async def cb_ai_csp_list(callback: CallbackQuery):
    """Список кастомных системных промптов канала."""
    cid = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        from app.repositories.custom_prompts import CustomSystemPromptsRepo

        repo = CustomSystemPromptsRepo(session)
        items = await repo.list_by_channel(cid)
    rows = []
    for p in items:
        prefix = "✅" if getattr(p, "is_active", False) else "☑️"
        title = (p.content or "").strip().replace("\n", " ")[:40] or "(пусто)"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {title}", callback_data=f"ai_csp_open_{cid}_{p.id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить промпт/шаблон", callback_data=f"ai_csp_add_{cid}"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")])
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "📚 Мои системные промпты\n\nВыберите промпт или добавьте новый.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("ai_csp_open_"))
async def cb_ai_csp_open(callback: CallbackQuery):
    """Экран одного промпта: вкл/выкл, редактировать, удалить."""
    parts = callback.data.split("_")
    cid = int(parts[-2])
    pid = int(parts[-1])
    async with AsyncSessionLocal() as session:
        from app.domain.models import AICustomSystemPrompt

        obj = await session.get(AICustomSystemPrompt, pid)
        if not obj or obj.channel_id != cid:
            return await callback.answer("❌ Промпт не найден", show_alert=True)
        is_active = bool(getattr(obj, "is_active", False))
        content = (obj.content or "").strip()
        status = "Активен" if is_active else "Не активен"
        text = f"📝 Промпт #{pid}\nСостояние: {status}\n\n{content[:2000]}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("Выкл" if is_active else "Вкл"),
                    callback_data=f"ai_csp_toggle_{cid}_{pid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Редактировать", callback_data=f"ai_csp_edit_{cid}_{pid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить", callback_data=f"ai_csp_del_{cid}_{pid}"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"ai_csp_list_{cid}")],
        ]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ai_csp_toggle_"))
async def cb_ai_csp_toggle(callback: CallbackQuery):
    """Переключить активность промпта (вкл/выкл)."""
    parts = callback.data.split("_")
    cid = int(parts[-2])
    pid = int(parts[-1])
    async with AsyncSessionLocal() as session:
        from app.domain.models import AICustomSystemPrompt
        from app.repositories.custom_prompts import CustomSystemPromptsRepo

        repo = CustomSystemPromptsRepo(session)
        obj = await session.get(AICustomSystemPrompt, pid)
        if not obj or obj.channel_id != cid:
            return await callback.answer("❌ Промпт не найден", show_alert=True)
        if obj.is_active:
            # деактивировать текущий
            obj.is_active = False
            await session.commit()
        else:
            # активировать этот и отключить другие
            await repo.set_active(cid, pid)
    with suppress(Exception):
        await cb_ai_csp_open(callback)


@router.callback_query(F.data.startswith("ai_csp_edit_"))
async def cb_ai_csp_edit(callback: CallbackQuery, state: FSMContext):
    """Редактирование существующего промпта через FSM."""
    parts = callback.data.split("_")
    cid = int(parts[-2])
    pid = int(parts[-1])
    await state.set_state(SettingsFSM.custom_system_input)
    await state.update_data(custom_prompt_cid=cid, csp_edit_id=pid)
    msg = (
        "Отправьте текст промпта сообщением.\n\n"
        "Подсказка переменных: {topic}, {tone}, {length}, {emoji_level}, {lang}, {hashtags}, {cta}, {brand}, {audience}, {channel_title}.\n"
        "Можно использовать и для шаблона (user prompt). Пример шаблона: 'Перепиши текст кратко и по делу: {topic}'."
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            msg,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="← Назад", callback_data=f"ai_csp_list_{cid}"
                        )
                    ]
                ]
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("ai_csp_add_"))
async def cb_ai_csp_add(callback: CallbackQuery, state: FSMContext):
    """Создание нового промпта через FSM."""
    cid = int(callback.data.split("_")[-1])
    await state.set_state(SettingsFSM.custom_system_input)
    await state.update_data(custom_prompt_cid=cid, csp_edit_id=0)
    msg = (
        "Отправьте текст нового промпта/шаблона сообщением.\n\n"
        "Подсказка переменных: {topic}, {tone}, {length}, {emoji_level}, {lang}, {hashtags}, {cta}, {brand}, {audience}, {channel_title}.\n"
        "Пример (шаблон пользователя): 'Перепиши текст кратко и по делу: {topic}'."
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            msg,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="← Назад", callback_data=f"ai_csp_list_{cid}"
                        )
                    ]
                ]
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("ai_csp_del_"))
async def cb_ai_csp_del(callback: CallbackQuery):
    """Удалить промпт и вернуться к списку."""
    parts = callback.data.split("_")
    int(parts[-2])
    pid = int(parts[-1])
    async with AsyncSessionLocal() as session:
        from app.repositories.custom_prompts import CustomSystemPromptsRepo

        repo = CustomSystemPromptsRepo(session)
        ok = await repo.delete(pid)
    await callback.answer("🗑 Удалено" if ok else "Не найдено", show_alert=False)
    with suppress(Exception):
        await cb_ai_csp_list(callback)


@router.callback_query(F.data.startswith("ai_custom_priority_"))
async def cb_ai_custom_priority(callback: CallbackQuery):
    """Переключить приоритет пресет/свой без миграций БД:
    Если сейчас используется пресет → очищаем preset_id (будет использоваться свой).
    Если сейчас используется свой → просим выбрать пресет (кнопка в меню)."""
    cid = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        if getattr(st, "preset_id", None):
            # переключаемся на пользовательский промпт: убираем пресет
            await repo.update_preset(cid, None)
            await callback.answer("✅ Теперь используется: Пользовательский промпт")
        else:
            await callback.answer("ℹ️ Выберите пресет в меню", show_alert=False)
    # Перерисуем меню «Свой промпт»
    with suppress(Exception):
        await cb_ai_custom_prompt(callback)


async def cb_ai_custom_prompt_from_message(message: Message, cid: int):
    """Перерисовать меню «Свой промпт» из контекста message."""
    # Больше не показываем промежуточное меню; сразу открываем список промптов
    async with AsyncSessionLocal() as session:
        from app.repositories.custom_prompts import CustomSystemPromptsRepo

        repo = CustomSystemPromptsRepo(session)
        items = await repo.list_by_channel(cid)
    rows = []
    for p in items:
        prefix = "✅" if getattr(p, "is_active", False) else "☑️"
        title = (p.content or "").strip().replace("\n", " ")[:40] or "(пусто)"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {title}", callback_data=f"ai_csp_open_{cid}_{p.id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить промпт/шаблон", callback_data=f"ai_csp_add_{cid}"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")])
    with suppress(Exception):
        await message.answer(
            "📚 Мои системные промпты\n\nВыберите промпт или добавьте новый.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


## Хендлер выбора языка удалён


@router.callback_query(F.data.startswith("ai_model_"))
async def cb_ai_model(callback: CallbackQuery):
    """Выбор модели."""
    int(callback.data.split("_")[-1])
    await callback.answer(
        "⚙️ Настройка модели\nРаздел временно отключён", show_alert=True
    )


# -- МОДЕЛИ: базовый список и пер-режимные --

## Модели: временно отключено (удалены вспомогательные функции)


## _render_ai_models_menu удалён


## cb_ai_model_set удалён


## cb_ai_model_modes удалён


## cb_ai_model_mode_pick удалён


## cb_ai_model_mode_set удалён


## cb_ai_model_mode_reset удалён


## Перенесено в routers/sources.py: cb_ai_source_add
async def cb_ai_source_add(callback: CallbackQuery):
    """Добавление источника."""
    int(callback.data.split("_")[-1])
    await callback.answer(
        "➕ Добавление источника\nРаздел в разработке", show_alert=True
    )


## Перенесено в routers/sources.py: cb_ai_source_list
async def cb_ai_source_list(callback: CallbackQuery):
    """Список источников."""
    int(callback.data.split("_")[-1])
    await callback.answer("📋 Список источников\nРаздел в разработке", show_alert=True)


@router.callback_query(F.data.startswith("neu_test_"))
async def cb_neu_test(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ]
        ]
    )
    try:
        await callback.message.edit_text(
            "🧪 Тест/черновик\nРаздел в разработке.", reply_markup=kb
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "settings_back_root")
async def cb_settings_back_root(callback: CallbackQuery):
    kb = await _build_root_settings_kb(callback.from_user.id)
    try:
        await callback.message.edit_text(
            "Меню настроек бота\nЗдесь можно настроить работу канала или чата и параметры самого бота",
            reply_markup=kb,
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == CB.GM_ADD_CHANNEL)
async def cb_add_channel(callback: CallbackQuery):
    text = "Выберите куда подключаем бота"
    await callback.message.edit_text(text)
    try:
        if await _should_show_reply_keyboard(callback.from_user):
            await callback.message.answer(
                "Выберите тип:", reply_markup=add_channel_kb()
            )
        else:
            await callback.message.answer(
                "Нижнее меню скрыто. Включите его в настройках интерфейса, чтобы использовать быстрый выбор канала/чата."
            )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == CB.GM_MY_CHANNELS)
async def cb_my_channels(callback: CallbackQuery):
    user = callback.from_user
    if not user:
        return await callback.answer()
    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        channels = ChannelsRepo(session)
        client = await clients.create_or_get(user.id, user.username, user.full_name)
        items = await channels.list_by_owner(client.id)
    if not items:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Главное меню", callback_data=CB.GM_GLOBAL_MENU
                    )
                ]
            ]
        )
        await callback.message.edit_text(
            "У вас нет добавленных каналов", reply_markup=kb
        )
        return await callback.answer()
    # Оформление списка + кнопки по каналам
    lines = ["📋 Список ваших каналов:\n"]
    kb_rows = []
    for ch in items:
        title = html.escape(ch.title or "Без названия")
        lines.append(f"• {title}\nID: <code>{ch.tg_chat_id}</code>\n")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=(ch.title or str(ch.tg_chat_id))[:30],
                    callback_data=f"channels_settings_{ch.id}",
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="Главное меню", callback_data=CB.GM_GLOBAL_MENU)]
    )
    text = "\n".join(lines)
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("channels_settings_"))
async def cb_channel_settings(callback: CallbackQuery):
    try:
        cid = int(callback.data.split("_")[-1])
    except Exception:
        return await callback.answer("Ошибка данных", show_alert=True)
    # Кнопка «Сервисные сообщения» — отдельное подменю
    btn_delete_service = InlineKeyboardButton(
        text="Сервисные сообщения", callback_data=f"{CB.OPEN_SERVICE_MENU}channel:{cid}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Управление постами", callback_data=f"settings_post_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Клонирование", callback_data=f"settings_graber_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 ИИ", callback_data=f"settings_neuropost_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Управление ботом", callback_data=f"bot_manage_{cid}"
                )
            ],
            [btn_delete_service],
            [
                InlineKeyboardButton(
                    text="Удалить канал", callback_data=f"settings_delete_{cid}"
                )
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data=CB.GM_MY_CHANNELS),
                InlineKeyboardButton(
                    text="Главное меню", callback_data=CB.GM_GLOBAL_MENU
                ),
            ],
        ]
    )
    await callback.message.edit_text("⚙️ Меню настроек канала", reply_markup=kb)
    await callback.answer()


def _service_labels(for_type: str) -> list[tuple[str, str]]:
    # key → human
    if for_type == "channel":
        return [
            ("pinned", "Закреплённое сообщение"),
            ("title", "Новое название канала"),
            ("photo_new", "Новое фото канала"),
            ("photo_del", "Фото канала удалено"),
            ("auto_delete", "Изменён таймер автоудаления"),
            ("giveaway_scheduled", "📅 Розыгрыш запланирован"),
            ("giveaway_started", "📅 Розыгрыш начался"),
            ("giveaway_ended", "📅 Розыгрыш завершён"),
            ("giveaway_winners", "📅 Подведены итоги розыгрыша"),
        ]
    # chat/supergroup defaults (можно расширить)
    return [
        ("new_member", "Новый участник"),
        ("left_member", "Участник вышел"),
        ("pinned", "Закреплённое сообщение"),
        ("title", "Новое название чата"),
        ("photo_new", "Новое фото чата"),
        ("photo_del", "Фото чата удалено"),
        ("migrate_to", "Перенос в чат"),
        ("migrate_from", "Перенос из чата"),
        ("video_chat_scheduled", "📹 Видеочат запланирован"),
        ("video_chat_started", "📹 Видеочат начался"),
        ("video_chat_ended", "📹 Видеочат закончился"),
        ("video_chat_participants_invited", "Приглашены участники видеочата"),
    ]


async def _render_service_menu(callback: CallbackQuery, for_type: str, cid: int):
    async with AsyncSessionLocal() as session:
        repo = ChannelSettingsRepo(session)
        flags = await repo.get_service_flags(cid)
    items = _service_labels(for_type)
    rows: list[list[InlineKeyboardButton]] = []
    # по 2 в ряд
    for i in range(0, len(items), 2):
        row_btns = []
        for key, human in items[i : i + 2]:
            enabled = bool(int(flags.get(key, 0)))
            text = ("✅ " if enabled else "☑️ ") + human
            row_btns.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"{CB.SERVICE_TOGGLE_PREFIX}{for_type}:{key}:{cid}",
                )
            )
        rows.append(row_btns)
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"channels_settings_{cid}")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "Установите, какие типы сервисных сообщений бот будет автоматически удалять из {}.".format(
        "канала" if for_type == "channel" else "чата"
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith(CB.OPEN_SERVICE_MENU))
async def cb_open_service_menu(callback: CallbackQuery):
    # open_service_menu_{type}:{cid}
    raw = callback.data.removeprefix(CB.OPEN_SERVICE_MENU)
    try:
        for_type, cid_str = raw.split(":", 1)
        cid = int(cid_str)
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)
    await _render_service_menu(callback, for_type, cid)
    await callback.answer()


@router.callback_query(F.data.startswith("ai_text_history_"))
async def cb_ai_text_history(callback: CallbackQuery):
    cid = int(callback.data.split("_")[-1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧹 Очистить текущий диалог",
                    callback_data=f"ai_clear_conv_current_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧽 Очистить все диалоги канала",
                    callback_data=f"ai_clear_conv_all_{cid}",
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"neu_text_{cid}")],
        ]
    )
    try:
        await callback.message.edit_text(
            "🗂 История диалога\n\nВыберите действие:", reply_markup=kb
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


async def _render_neu_text_menu(callback: CallbackQuery, cid: int) -> None:
    """Перерисовать меню настроек текста ИИ по явному cid."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выбрать стиль", callback_data=f"ai_choose_preset_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Мои системные промпты", callback_data=f"ai_csp_list_{cid}"
                )
            ],
            [
                InlineKeyboardButton(text="Тон", callback_data=f"ai_tone_{cid}"),
                InlineKeyboardButton(text="Длина", callback_data=f"ai_length_{cid}"),
            ],
            [InlineKeyboardButton(text="Эмодзи", callback_data=f"ai_emoji_{cid}")],
            [
                InlineKeyboardButton(
                    text="История диалога", callback_data=f"ai_text_history_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text(
            "📝 Настройки генерации текста", reply_markup=kb
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith(CB.SERVICE_TOGGLE_PREFIX))
async def cb_service_toggle(callback: CallbackQuery):
    # service_toggle_{type}:{key}:{cid}
    raw = callback.data.removeprefix(CB.SERVICE_TOGGLE_PREFIX)
    try:
        for_type, key, cid_str = raw.split(":", 2)
        cid = int(cid_str)
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)
    async with AsyncSessionLocal() as session:
        repo = ChannelSettingsRepo(session)
        current = await repo.get_service_flags(cid)
        new_val = 0 if int(current.get(key, 0)) == 1 else 1
        await repo.set_service_flag(cid, key, bool(new_val))
    # перерисуем только клавиатуру
    await _render_service_menu(callback, for_type, cid)
    await callback.answer()


@router.callback_query(F.data.startswith("ai_clear_conv_"))
async def cb_ai_clear_conv(callback: CallbackQuery):
    # ai_clear_conv_current_{cid} | ai_clear_conv_all_{cid}
    raw = callback.data
    try:
        action, cid_str = raw.rsplit("_", 1)
        cid = int(cid_str)
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)
    user_id = int(callback.from_user.id)
    async with AsyncSessionLocal() as session:
        import hashlib
        from app.repositories.ai_settings import ChannelAISettingsRepo

        gen = AIGenerationService(session)
        deleted = 0
        if action.startswith("ai_clear_conv_current_"):
            # построим prompt_key из активных настроек
            ai_repo = ChannelAISettingsRepo(session)
            ai_set = await ai_repo.get_or_create(cid)
            priority = (
                "preset"
                if ai_set.preset_id
                else ("custom" if (ai_set.custom_prompt or "").strip() else "default")
            )
            base = f"{priority}|*|{ai_set.preset_id or ''}|{(ai_set.custom_prompt or '').strip()}|{(ai_set.user_prompt_template or '').strip()}|{(ai_set.model or '').strip()}"
            pkey = hashlib.sha1(base.encode("utf-8")).hexdigest()
            deleted = await gen.clear_history(user_id=user_id, prompt_key=pkey)
        else:
            # очистить все диалоги, связанные с каналом
            deleted = await gen.clear_history(user_id=user_id, channel_id=cid)
    await callback.answer(f"Очищено диалогов: {deleted}", show_alert=True)
    # Вернёмся в подменю истории, чтобы не смешивать с основным меню
    return await cb_ai_text_history(callback)


async def cb_settings_tz(callback: CallbackQuery, state: FSMContext):
    cid = int(callback.data.split("_")[-1])
    # Попробуем достать текущий tz из настроек
    from app.repositories.settings import ChannelSettingsRepo

    async with AsyncSessionLocal() as session:
        repo = ChannelSettingsRepo(session)
        st = await repo.get_by_channel_id(cid)
    cur_tz = None
    if st and st.filters and isinstance(st.filters, dict):
        cur_tz = st.filters.get("tz")
    # Определим текущее смещение в минутах
    cur_min = _offset_minutes_from_tz(cur_tz)

    # Построим клавиатуру по всем часовым смещениям от -12 до +14
    def _fmt_offset(mins: int) -> str:
        sign = "+" if mins >= 0 else "-"
        mins = abs(mins)
        h = mins // 60
        return f"UTC{sign}{h:02d}"

    rows: list[list[InlineKeyboardButton]] = []
    cols = 4
    current_label = _fmt_offset(cur_min)
    items: list[tuple[str, int]] = []
    for h in range(-12, 15):
        items.append((_fmt_offset(h * 60), h * 60))
    # разложим по рядам
    row: list[InlineKeyboardButton] = []
    for label, val in items:
        mark = "✅ " if label == current_label else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{label}", callback_data=f"tz_set_offset:{cid}:{val}"
            )
        )
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="Назад", callback_data=f"channels_settings_{cid}")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = (
        "🌍 Часовой пояс\n"
        f"Текущий: <b>{current_label}</b>{' (Europe/Moscow)' if current_label == 'UTC+03' else ''}.\n"
        "Выберите смещение UTC."
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


## Перенесено в routers/tz.py: cb_tz_pick
## Перенесено в routers/tz.py: cb_tz_set_offset
@router.callback_query(F.data.startswith("tz_set_global:"))
## Перенесено в routers/tz.py: cb_tz_set_global
## Перенесено в routers/tz.py: on_tz_search_input
# --- Подменю: Управление постами --- перенесено в routers/settings.py
# --- Подменю: Управление заявками --- перенесено в routers/settings.py
# --- Подменю: Клонирование/Грабер --- перенесено в routers/settings.py
# --- Удаление канала --- перенесено в routers/settings.py
@router.callback_query(F.data == CB.GM_GLOBAL_MENU)
async def cb_global_menu(callback: CallbackQuery):
    await cmd_start(callback.message)
    await callback.answer()


async def _render_forward_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    forward_to = set(int(x) for x in (data.get("forward_to") or []))
    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        channels = ChannelsRepo(session)
        client = await clients.create_or_get(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.full_name,
        )
        items = await channels.list_by_owner(client.id)
    rows: list[list[InlineKeyboardButton]] = []
    for ch in items or []:
        title = (ch.title or str(ch.tg_chat_id))[:40]
        mark = "✅ " if int(ch.id) in forward_to else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{title}",
                    callback_data=f"{CB.POST_FWD_TOGGLE_PREFIX}{ch.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Все", callback_data=CB.POST_FWD_ALL),
            InlineKeyboardButton(text="Ни один", callback_data=CB.POST_FWD_NONE),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_SETTINGS_BACK)]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Выберите каналы для пересылки:", reply_markup=kb
        )
    # Отметим, что мы находимся в подменю пересылки
    with suppress(Exception):
        await state.update_data(ui_submenu="forward")


## moved to posting_publish: cb_post_settings_forward
## Перенесено в routers/settings.py: cb_post_replace_autosign
@router.callback_query(F.data.startswith(CB.POST_FWD_TOGGLE_PREFIX))
async def cb_post_fwd_toggle(callback: CallbackQuery, state: FSMContext):
    try:
        t_cid = int(callback.data.removeprefix(CB.POST_FWD_TOGGLE_PREFIX))
    except Exception:
        return await callback.answer()
    data = await state.get_data()
    cur = set(int(x) for x in (data.get("forward_to") or []))
    if t_cid in cur:
        cur.remove(t_cid)
    else:
        cur.add(t_cid)
    await state.update_data(forward_to=list(cur))
    await _render_forward_menu(callback, state)
    await callback.answer()


## moved to posting_publish: cb_post_fwd_all


## moved to posting_publish: cb_post_fwd_none


## moved to posting_publish: cb_post_settings_defer


## moved to routers/utils/time_utils.py: _build_defer_calendar_kb


@router.callback_query(F.data == "defer_expand_cal")
async def cb_defer_expand(callback: CallbackQuery, state: FSMContext):
    from datetime import datetime as _dt

    data = await state.get_data()
    chan_id = int(data.get("channel_id") or 0)
    center_iso = data.get("defer_center")
    selected_iso = data.get("defer_selected") or center_iso
    if not center_iso:
        center_iso = _dt.now().date().isoformat()
    center = _dt.fromisoformat(center_iso)
    selected = _dt.fromisoformat(selected_iso)
    kb = _build_defer_calendar_kb(chan_id, center, selected, expanded=True)
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=kb)
    await state.update_data(defer_expanded=True)
    await callback.answer()


@router.callback_query(
    F.data.startswith("defer_month_prev:") | F.data.startswith("defer_month_next:")
)
async def cb_defer_month_shift(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    action, cid_str, d_iso = parts
    from datetime import datetime as _dt

    focus = _dt.fromisoformat(d_iso)
    if action.startswith("defer_month_prev"):
        year = focus.year if focus.month > 1 else focus.year - 1
        month = focus.month - 1 if focus.month > 1 else 12
        focus = _dt(year, month, 1)
    else:
        year = focus.year if focus.month < 12 else focus.year + 1
        month = focus.month + 1 if focus.month < 12 else 1
        focus = _dt(year, month, 1)
    data = await state.get_data()
    selected_iso = data.get("defer_selected")
    selected = _dt.fromisoformat(selected_iso) if selected_iso else focus
    cid = int(cid_str)
    expanded = bool(data.get("defer_expanded"))
    kb = _build_defer_calendar_kb(cid, focus, selected, expanded=expanded)
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=kb)
    await state.update_data(defer_center=focus.date().isoformat())
    await callback.answer()


@router.callback_query(F.data.startswith("defer_pick_day:"))
async def cb_defer_pick_day(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    _, cid_str, d_iso = parts
    from datetime import datetime as _dt

    selected = _dt.fromisoformat(d_iso)
    data = await state.get_data()
    center_iso = data.get("defer_center") or d_iso
    center = _dt.fromisoformat(center_iso)
    data2 = await state.get_data()
    expanded = bool(data2.get("defer_expanded"))
    cid = int(cid_str)
    kb = _build_defer_calendar_kb(cid, center, selected, expanded=expanded)
    # Перерисовываем текст (дата/список постов) и клавиатуру одновременно
    text = await _render_defer_header_text(cid, selected.date())
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(defer_selected=d_iso)
    await callback.answer()


@router.callback_query(F.data.startswith("cp_pick_day:"))
async def cb_cp_pick_day(callback: CallbackQuery, state: FSMContext):
    # Если мы в режиме отложенной публикации — после выбора даты попросим время
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    _, cid_str, d_iso = parts
    data = await state.get_data()
    if data.get("ui_submenu") == "defer":
        with suppress(Exception):
            await state.update_data(defer_date=d_iso)
        # Определим подпись часового пояса канала
        from app.repositories.settings import ChannelSettingsRepo as _SettingsRepo

        tz_label = "GMT+0"
        try:
            async with AsyncSessionLocal() as session:
                repo = _SettingsRepo(session)
                st = await repo.get_by_channel_id(int(data.get("channel_id", 0) or 0))
                code = (st.filters or {}).get("tz") if (st and st.filters) else None
                from datetime import datetime as _dt, timezone as _tz
                from zoneinfo import ZoneInfo

                loc = ZoneInfo(code) if code else _tz.utc
                off = int(
                    (
                        _dt.now(loc).utcoffset()
                        or _dt.now(_tz.utc).utcoffset()
                        or _dt.now(_tz.utc) - _dt.now(_tz.utc)
                    ).total_seconds()
                    // 3600
                )
                name = (
                    "Москва"
                    if (code and ("Europe/Moscow" in code or "Moscow" in code))
                    else (code or "UTC")
                )
                sign = "+" if off >= 0 else "-"
                tz_label = f"GMT{sign}{abs(off)} {name}"
        except Exception:
            pass
        # Перерисуем текущее сообщение: сохраняем текст‑инструкцию, обновляем календарь (focus и selected = выбранный день)
        from datetime import datetime as _dt

        focus = _dt.fromisoformat(d_iso)
        text = (
            f"Выбранная дата: {focus.strftime('%d.%m.%Y')}\n\n"
            f"Отправьте время выхода поста в вашем часовом поясе ({tz_label}) в формате:\n"
            "18:30\n18 30\n1830"
        )
        kb = _build_calendar_kb(int(cid_str), focus, focus)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=kb)
        # Переходим к ожиданию времени
        await state.set_state(PostFSM.defer_time_input)
        return await callback.answer()
    # Иначе — старый сценарий календаря (контент‑план)
    from datetime import datetime as _dt
    from app.bot.routers.content_plan import _render_content_plan

    cid = int(cid_str)
    center = _dt.fromisoformat(d_iso)
    await _render_content_plan(callback, state, cid, center)
    await callback.answer()


## moved to routers/utils/time_utils.py: _parse_time_hhmm


async def _handle_autodelete_free_mode(
    message: Message, state: FSMContext, data0: dict, text: str
) -> bool:
    if not data0.get("_awaiting_autodelete_free"):
        return False
    from app.bot.routers.shared import parse_duration_free as _p
    from app.bot.routers.post_editor import _format_duration_label as _lab

    sec = _p(text)
    if not sec:
        await message.reply("Неверный формат. Примеры: 6ч, 12ч, 2д 6ч, 14д")
        return True
    chan_id_gate = int(data0.get("channel_id") or 0)
    is_pro_gate = await _is_channel_pro(chan_id_gate)
    if (not is_pro_gate) and int(sec) < 5 * 3600:
        kb = await _build_pro_offer_kb(chan_id_gate, message)
        with suppress(TelegramBadRequest):
            await message.reply(
                "Короткие таймеры доступны в Pro. В Free — только таймеры от 5 часов и дольше.",
                reply_markup=kb,
            )
        return True
    payload = await _apply_autodelete_payload(state, data0, int(sec), _lab)
    if await _maybe_return_to_cp_card(message, state):
        return True
    await _update_settings_menu_after_autodel(message, state, payload)
    return True


async def _is_channel_pro(chan_id: int) -> bool:
    try:
        if not chan_id:
            return False
        async with AsyncSessionLocal() as _sg:
            from app.repositories.channels import ChannelsRepo as _ChRepo
            from app.domain.models import Client as _Client

            ch_g = await _ChRepo(_sg).get_by_id(chan_id)
            if ch_g:
                owner_g = await _sg.get(_Client, int(getattr(ch_g, "owner_id", 0)))
                return bool(getattr(owner_g, "is_premium", False)) if owner_g else False
        return False
    except Exception:
        return False


async def _build_pro_offer_kb(chan_id_gate: int, message: Message):
    from app.core.config import settings as _settings

    admin_username = getattr(_settings, "admin_username", None) or "vasilyiusii"
    admin_url = f"https://t.me/{admin_username}"
    ch_title_gate = None
    try:
        async with AsyncSessionLocal() as _sg2:
            from app.repositories.channels import ChannelsRepo as _ChRepo2

            ch2 = await _ChRepo2(_sg2).get_by_id(chan_id_gate)
            if ch2:
                ch_title_gate = ch2.title or str(ch2.tg_chat_id)
    except Exception:
        pass
    import urllib.parse as _urlparse

    text_tpl = f"Здравствуйте, пишу по поводу подписки Pro. Хочу приобрести. Канал: {ch_title_gate or chan_id_gate} (ID: {chan_id_gate}). Мой ник: @{message.from_user.username or ''}."
    share_url = f"https://t.me/share/url?url={_urlparse.quote_plus(admin_url)}&text={_urlparse.quote_plus(text_tpl)}"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    rows = [
        [InlineKeyboardButton(text="Приобрести Pro", url=admin_url)],
        [InlineKeyboardButton(text="Отправить заявку", url=share_url)],
        [
            InlineKeyboardButton(
                text="Подробнее о Pro", callback_data="settings_subscription"
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _apply_autodelete_payload(
    state: FSMContext, data0: dict, seconds: int, _lab
) -> dict:
    payload = dict(data0.get("payload") or {})
    payload["autodelete_seconds"] = int(seconds)
    payload["autodelete_label"] = _lab(int(seconds))
    payload.pop("autodelete_views", None)
    await state.update_data(payload=payload, _awaiting_autodelete_free=False)
    return payload


async def _maybe_return_to_cp_card(message: Message, state: FSMContext) -> bool:
    try:
        st_all = await state.get_data()
        mark = st_all.get("cp_back_to_card") or st_all.get("return_to_notice") or {}
        post_id = mark.get("post_id")
        date_iso = mark.get("date")
        if post_id and date_iso:
            from app.bot.routers.post_editor import (
                _persist_autodelete_if_cp as _persist,
            )

            await _persist(state)
            with suppress(Exception):
                await state.update_data(cp_back_to_card=None)
            try:
                st = await state.get_data()
                m_id = st.get("autodel_menu_msg_id")
                if m_id:
                    with suppress(TelegramBadRequest):
                        await tg_bot.delete_message(
                            chat_id=message.chat.id, message_id=int(m_id)
                        )
                with suppress(TelegramBadRequest):
                    await message.delete()
            except Exception:
                pass
            from app.bot.routers.content_plan import cb_cp_open_post as _open_cp

            class _Cb:
                def __init__(self, msg, data):
                    self.message = msg
                    self.data = data

                async def answer(self, *args, **kwargs):
                    return None

            cb2 = _Cb(message, f"cp_open_post:{post_id}:{date_iso}")
            await _open_cp(cb2, state)
            return True
    except Exception:
        pass
    return False


async def _update_settings_menu_after_autodel(
    message: Message, state: FSMContext, payload: dict
) -> None:
    try:
        st2 = await state.get_data()
        secs = int((payload.get("autodelete_seconds") or 0))
        views = int((payload.get("autodelete_views") or 0))
        from app.bot.keyboards.posting import settings_menu_kb

        kb = settings_menu_kb(
            timer_set=bool(secs),
            repeat_on=bool(st2.get("repeat_on", False)),
            time_seconds=secs,
            views_value=views,
            notify_on=bool(st2.get("notify_on", True)),
            autosign_on=bool(st2.get("autosign_on", False)),
            pin_on=bool(st2.get("pin_on", False)),
            comments_on=bool(st2.get("comments_on", True)),
        )
        m_id = st2.get("autodel_menu_msg_id") or st2.get("settings_msg_id")
        if m_id:
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=int(m_id),
                    text="⚙️ Настройки публикации",
                    reply_markup=kb,
                )
            with suppress(Exception):
                await state.update_data(
                    settings_msg_id=int(m_id), ui_submenu="settings"
                )
        with suppress(TelegramBadRequest):
            await message.delete()
    except Exception:
        pass


@router.message(PostFSM.defer_time_input)
async def on_defer_time_input(message: Message, state: FSMContext):
    # Возможны два сценария этого состояния: ввод времени отложенной публикации
    # или свободный ввод таймера автоудаления (маркер _awaiting_autodelete_free в state)
    text = (message.text or "").strip().lower()
    data0 = await state.get_data()
    # Базовые переменные
    from datetime import datetime as _dt

    data = data0
    chan_id = int(data.get("channel_id") or 0)
    defer_date_iso = data.get("defer_date")
    base_date = _dt.fromisoformat(defer_date_iso) if defer_date_iso else _dt.now()
    if await _handle_autodelete_free_mode(message, state, data0, text):
        return
    when_local: _dt | None = None
    if text.startswith("сегодня"):
        rest = text.replace("сегодня", "").strip()
        hm = _parse_time_hhmm(rest)
        if hm:
            when_local = base_date.replace(
                hour=hm[0], minute=hm[1], second=0, microsecond=0
            )
    elif text.startswith("завтра"):
        rest = text.replace("завтра", "").strip()
        hm = _parse_time_hhmm(rest)
        if hm:
            when_local = (base_date + timedelta(days=1)).replace(
                hour=hm[0], minute=hm[1], second=0, microsecond=0
            )
    else:
        m = re.match(r"^(\d{1,2})\.(\d{1,2})\s+(\d{1,2}:\d{2})$", text)
        if m:
            dd = int(m.group(1))
            mm = int(m.group(2))
            hm = _parse_time_hhmm(m.group(3))
            year = base_date.year
            if hm and 1 <= mm <= 12 and 1 <= dd <= 31:
                when_local = _dt(year, mm, dd, hm[0], hm[1])
        else:
            hm = _parse_time_hhmm(text)
            if hm:
                when_local = base_date.replace(
                    hour=hm[0], minute=hm[1], second=0, microsecond=0
                )
    if when_local is None:
        return await message.reply(
            "Не понял время. Примеры: 18:30, сегодня 19:00, 25.09 10:00"
        )
    when_utc_aware, when_utc_naive = await _local_to_utc_for_channel(
        chan_id, when_local
    )
    # Проверка: время уже прошло?
    now_utc = _dt.utcnow()
    if when_utc_naive <= now_utc:
        return await message.reply(
            "Ошбка: дата публикации уже прошла. Пожалуйста, выберите будущее время."
        )
    # Сохраняем задачу в контент‑план
    await _schedule_post_to_targets(chan_id, data, message, when_utc_aware)
    # Подтверждение и кнопка «Открыть в контент‑плане»
    text_confirm, kb = await _build_defer_confirmation(chan_id, when_local, data)
    await message.reply(
        text_confirm,
        reply_markup=kb,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    return
