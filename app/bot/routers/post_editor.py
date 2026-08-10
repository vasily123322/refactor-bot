from aiogram import Router, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
)
from aiogram.fsm.context import FSMContext
from contextlib import suppress
from loguru import logger
from app.bot.fsm.states import PostFSM
from app.core.callbacks import CB
from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.bot.routers.main import _try_handle_global_reply  # reuse existing helper
from app.bot.bot_instance import bot as tg_bot
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from app.bot.keyboards.posting import (
    post_actions,
    settings_menu_kb,
    _format_duration_label,
)
from app.bot.keyboards.builders import (
    build_media_menu_kb,
    build_back_kb,
    build_preview_menu_kb,
)
from app.domain.models import PostTask
from app.domain.models import Client
from app.core.config import settings
import urllib.parse as _urlparse
from app.bot.routers.shared import apply_preview_media as _apply_preview_media
from app.bot.routers.shared import build_preview_kb as _build_preview_kb
from app.bot.routers.shared import is_ai_enabled as _is_ai_enabled
from app.bot.routers.shared import (
    back_restore_preview_from_caption as _back_from_caption,
)
from app.bot.routers.shared import (
    back_restore_preview_from_content as _back_from_content,
)
from app.bot.routers.shared import (
    build_payload_from_message as _build_payload_from_message,
)
from app.bot.routers.shared import (
    resolve_original_post_ref as _resolve_original_post_ref,
)


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

# ---- Таймер удаления: меню и обработчики ----


@router.callback_query(F.data == CB.POST_SETTINGS_TIMER)
async def cb_post_timer_menu(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    cur_sec = int(payload.get("autodelete_seconds") or 0)
    _format_duration_label(cur_sec)
    report_on = bool(payload.get("autodelete_report", False))
    title = (
        "🗑 ТАЙМЕР УДАЛЕНИЯ\n\n"
        "Вы можете задать время, через которое пост будет автоматически удалён.\n\n"
        "Для этого достаточно отправить боту необходимый период, либо выбрать один из предложенных вариантов.\n\n"
        "6 — удалить через 6 ч\n"
        "6:30 — удалить через 6 ч 30 мин\n"
        "6 30 — удалить через 6 ч 30 мин\n"
        "10д — удалить через 10 дней"
    )
    # пресеты как на макете: минуты/часы/дни
    rows: list[list[InlineKeyboardButton]] = []
    # Переключатель на меню «по просмотрам»
    rows.append(
        [
            InlineKeyboardButton(
                text="👁 Удаление по просмотрам", callback_data=CB.POST_VIEW_MENU
            )
        ]
    )
    # Ряд с «нет» и минутами
    rows.append(
        [
            InlineKeyboardButton(text="нет", callback_data=CB.POST_TIMER_CLEAR),
            InlineKeyboardButton(
                text="5 мин", callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{5 * 60}"
            ),
            InlineKeyboardButton(
                text="15 мин", callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{15 * 60}"
            ),
            InlineKeyboardButton(
                text="30 мин", callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{30 * 60}"
            ),
        ]
    )
    # Часы: 1,2,4
    rows.append(
        [
            InlineKeyboardButton(
                text="1 ч", callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{1 * 3600}"
            ),
            InlineKeyboardButton(
                text="2 ч", callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{2 * 3600}"
            ),
            InlineKeyboardButton(
                text="4 ч", callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{4 * 3600}"
            ),
        ]
    )
    # Часы/дни: 12ч, 18ч, 1д
    rows.append(
        [
            InlineKeyboardButton(
                text="12 ч", callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{12 * 3600}"
            ),
            InlineKeyboardButton(
                text="18 ч", callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{18 * 3600}"
            ),
            InlineKeyboardButton(
                text="1 д",
                callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{1 * 24 * 3600}",
            ),
        ]
    )
    # Дни: 2,3,4
    rows.append(
        [
            InlineKeyboardButton(
                text="2 д",
                callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{2 * 24 * 3600}",
            ),
            InlineKeyboardButton(
                text="3 д",
                callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{3 * 24 * 3600}",
            ),
            InlineKeyboardButton(
                text="4 д",
                callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{4 * 24 * 3600}",
            ),
        ]
    )
    # Дни: 5,7
    rows.append(
        [
            InlineKeyboardButton(
                text="5 д",
                callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{5 * 24 * 3600}",
            ),
            InlineKeyboardButton(
                text="7 д",
                callback_data=f"{CB.POST_TIMER_PRESET_PREFIX}{7 * 24 * 3600}",
            ),
        ]
    )
    # Отчёт-тумблер и управление
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "✅ Прислать отчёт об удалении"
                    if report_on
                    else "☑️ Прислать отчёт об удалении"
                ),
                callback_data=CB.POST_TIMER_REPORT,
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_SETTINGS_BACK)]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            title, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    # пометим подменю настроек и запомним id сообщения меню автудаления
    with suppress(Exception):
        await state.update_data(
            ui_submenu="autodel", autodel_menu_msg_id=callback.message.message_id
        )
    # включим режим свободного ввода интервала сразу, без отдельной кнопки
    await state.set_state(PostFSM.defer_time_input)
    await state.update_data(_awaiting_autodelete_free=True)
    await callback.answer()


@router.callback_query(F.data == CB.POST_VIEW_MENU)
async def cb_post_view_menu(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    int(payload.get("autodelete_views") or 0)
    report_on = bool(payload.get("autodelete_report", False))
    title = (
        "👁 УДАЛЕНИЕ ПО ПРОСМОТРАМ\n\n"
        "Вы можете установить после какого количества просмотров пост будет удалён.\n"
        "Отправьте боту количество просмотров либо выберите один из предложенных вариантов.\n\n"
        "1500 — удалить после 1500 просмотров\n"
        "3к — удалить после 3000 просмотров"
    )
    rows: list[list[InlineKeyboardButton]] = []
    # Переключатель обратно на таймер
    rows.append(
        [
            InlineKeyboardButton(
                text="🕒 Таймер удаления", callback_data=CB.POST_SETTINGS_TIMER
            )
        ]
    )
    # Первый ряд: нет и мелкие значения
    rows.append(
        [
            InlineKeyboardButton(
                text="нет", callback_data=f"{CB.POST_VIEW_PRESET_PREFIX}0"
            ),
            InlineKeyboardButton(
                text="200", callback_data=f"{CB.POST_VIEW_PRESET_PREFIX}200"
            ),
            InlineKeyboardButton(
                text="500", callback_data=f"{CB.POST_VIEW_PRESET_PREFIX}500"
            ),
            InlineKeyboardButton(
                text="1к", callback_data=f"{CB.POST_VIEW_PRESET_PREFIX}1000"
            ),
        ]
    )

    # Остальные по 4 в ряд
    def _label(v: int) -> str:
        return f"{v // 1000}к" if v >= 1000 else str(v)

    grid = [
        [2000, 3000, 5000, 10000],
        [20000, 30000, 50000, 100000],
        [200000, 300000],
    ]
    for line in grid:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_label(v), callback_data=f"{CB.POST_VIEW_PRESET_PREFIX}{v}"
                )
                for v in line
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "✅ Прислать отчёт об удалении"
                    if report_on
                    else "☑️ Прислать отчёт об удалении"
                ),
                callback_data=CB.POST_VIEW_REPORT,
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_SETTINGS_BACK)]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            title, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    with suppress(Exception):
        await state.update_data(
            ui_submenu="autodel", autodel_menu_msg_id=callback.message.message_id
        )
    # включим режим свободного ввода просмотров сразу
    await state.set_state(PostFSM.defer_time_input)
    await state.update_data(_awaiting_autodelete_views=True)
    await callback.answer()


@router.callback_query(F.data.startswith(CB.POST_VIEW_PRESET_PREFIX))
async def cb_post_view_preset(callback: CallbackQuery, state: FSMContext):
    try:
        views = int(callback.data.replace(CB.POST_VIEW_PRESET_PREFIX, ""))
    except Exception:
        return await callback.answer("Ошибка", show_alert=False)
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    payload["autodelete_views"] = int(views)
    # взаимоисключаемость: очистим таймер
    payload.pop("autodelete_seconds", None)
    payload.pop("autodelete_label", None)
    await state.update_data(payload=payload)
    # Если пришли из карточки контент‑плана — сохраним и вернём карточку
    post_id, date_iso = await _persist_autodelete_if_cp(state)
    if post_id and date_iso:
        try:
            from app.bot.routers.content_plan import cb_cp_open_post as _open_cp

            cb2 = callback.model_copy(
                update={"data": f"cp_open_post:{post_id}:{date_iso}"}
            )  # type: ignore
            await _open_cp(cb2, state)
        except Exception:
            pass
        return await callback.answer(
            "👁 Удаление по просмотрам: установлено", show_alert=True
        )
    # Иначе — вернуться к настройкам публикации
    data2 = await state.get_data()
    from app.bot.keyboards.posting import settings_menu_kb

    secs = int(payload.get("autodelete_seconds") or 0)
    views = int(payload.get("autodelete_views") or 0)
    kb = settings_menu_kb(
        timer_set=bool(secs),
        repeat_on=bool(data2.get("repeat_on", False)),
        time_seconds=secs,
        views_value=views,
        notify_on=bool(data2.get("notify_on", True)),
        autosign_on=bool(data2.get("autosign_on", False)),
        pin_on=bool(data2.get("pin_on", False)),
        comments_on=bool(data2.get("comments_on", True)),
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("⚙️ Настройки публикации", reply_markup=kb)
    await callback.answer("Установлено")


@router.callback_query(F.data == CB.POST_VIEW_REPORT)
async def cb_post_view_report(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    payload["autodelete_report"] = not bool(payload.get("autodelete_report", False))
    await state.update_data(payload=payload)
    await cb_post_view_menu(callback, state)


@router.callback_query(F.data == CB.POST_VIEW_CLEAR)
async def cb_post_view_clear(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    payload.pop("autodelete_views", None)
    await state.update_data(payload=payload)
    post_id, date_iso = await _persist_autodelete_if_cp(state)
    if post_id and date_iso:
        try:
            from app.bot.routers.content_plan import cb_cp_open_post as _open_cp

            cb2 = callback.model_copy(
                update={"data": f"cp_open_post:{post_id}:{date_iso}"}
            )  # type: ignore
            await _open_cp(cb2, state)
        except Exception:
            pass
        return await callback.answer(
            "👁 Удаление по просмотрам: очищено", show_alert=True
        )
    # Вернуться в настройки публикации
    data2 = await state.get_data()
    from app.bot.keyboards.posting import settings_menu_kb

    secs = int(payload.get("autodelete_seconds") or 0)
    kb = settings_menu_kb(
        timer_set=bool(secs),
        repeat_on=bool(data2.get("repeat_on", False)),
        time_seconds=secs,
        views_value=0,
        notify_on=bool(data2.get("notify_on", True)),
        autosign_on=bool(data2.get("autosign_on", False)),
        pin_on=bool(data2.get("pin_on", False)),
        comments_on=bool(data2.get("comments_on", True)),
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("⚙️ Настройки публикации", reply_markup=kb)
    await callback.answer("Очищено")


async def _cb_timer_short_preset_gate(
    callback: CallbackQuery, state: FSMContext, sec: int
) -> bool:
    try:
        data = await state.get_data()
        chan_id = int(data.get("channel_id") or 0)
        is_pro = False
        if chan_id:
            async with AsyncSessionLocal() as s:
                ch = await ChannelsRepo(s).get_by_id(chan_id)
                if ch:
                    owner = await s.get(Client, int(getattr(ch, "owner_id", 0)))
                    is_pro = (
                        bool(getattr(owner, "is_premium", False)) if owner else False
                    )
        if (not is_pro) and int(sec) < 5 * 3600:
            admin_url = (
                f"https://t.me/{settings.admin_username}"
                if settings.admin_username
                else "https://t.me/vasilyiusii"
            )
            ch_title = None
            if chan_id:
                try:
                    async with AsyncSessionLocal() as s2:
                        ch2 = await ChannelsRepo(s2).get_by_id(chan_id)
                        if ch2:
                            ch_title = ch2.title or str(ch2.tg_chat_id)
                except Exception:
                    pass
            text_tpl = (
                f"Здравствуйте, пишу по поводу подписки Pro. Хочу приобрести. Канал: {ch_title or chan_id} (ID: {chan_id}). "
                f"Мой ник: @{callback.from_user.username or ''}."
            )
            share_url = f"https://t.me/share/url?url={_urlparse.quote_plus(admin_url)}&text={_urlparse.quote_plus(text_tpl)}"
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            rows = [
                [InlineKeyboardButton(text="Оформить Pro — 990 ₽/мес", url=admin_url)],
                [InlineKeyboardButton(text="Отправить заявку", url=share_url)],
                [
                    InlineKeyboardButton(
                        text="Что входит в Pro", callback_data="settings_subscription"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="← Назад", callback_data=CB.POST_SETTINGS_BACK
                    )
                ],
            ]
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            with suppress(TelegramBadRequest):
                await callback.message.edit_text(
                    "Короткие таймеры доступны в Pro. В Free — только таймеры от 5 часов и дольше.",
                    reply_markup=kb,
                )
            # Лог: попытка короткого таймера на Free
            try:
                async with AsyncSessionLocal() as _slog:
                    from app.repositories.admin import AdminConfigRepo as _AdminRepo
                    from app.repositories.channels import ChannelsRepo as _ChRepo

                    log_chat_id = await _AdminRepo(_slog).get_log_chat_id()
                    if log_chat_id:
                        u_link = (
                            f"https://t.me/{callback.from_user.username}"
                            if callback.from_user.username
                            else f"tg://user?id={callback.from_user.id}"
                        )
                        chan_link = None
                        chx = await _ChRepo(_slog).get_by_id(int(chan_id))
                        if chx:
                            try:
                                chat = await tg_bot.get_chat(int(chx.tg_chat_id))
                                uname = getattr(chat, "username", None)
                                if uname:
                                    chan_link = f"https://t.me/{uname}"
                            except Exception:
                                chan_link = None
                        txt = (
                            f"Gate hit: feature=autodelete_short_preset user={u_link} "
                            f"channel={(chan_link or chan_id)} seconds={sec}"
                        )
                        with suppress(Exception):
                            await tg_bot.send_message(
                                int(log_chat_id), txt, disable_web_page_preview=True
                            )
            except Exception:
                pass
            await callback.answer()
            return True
    except Exception:
        pass
    return False


async def _get_channel_pro_info(state: FSMContext) -> tuple[int, bool, str | None]:
    try:
        data = await state.get_data()
        chan_id = int(data.get("channel_id") or 0)
        is_pro = False
        ch_title: str | None = None
        if chan_id:
            async with AsyncSessionLocal() as s:
                ch = await ChannelsRepo(s).get_by_id(chan_id)
                if ch:
                    owner = await s.get(Client, int(getattr(ch, "owner_id", 0)))
                    is_pro = (
                        bool(getattr(owner, "is_premium", False)) if owner else False
                    )
                    ch_title = getattr(ch, "title", None) or str(
                        getattr(ch, "tg_chat_id", "")
                    )
        return chan_id, is_pro, ch_title
    except Exception:
        return 0, False, None


async def _pro_gate_show_ui_and_log(
    callback: CallbackQuery,
    chan_id: int,
    ch_title: str | None,
    main_text: str,
    feature: str,
    sec: int | None = None,
) -> None:
    admin_url = (
        f"https://t.me/{settings.admin_username}"
        if settings.admin_username
        else "https://t.me/vasilyiusii"
    )
    text_tpl = (
        f"Здравствуйте, пишу по поводу подписки Pro. Хочу приобрести. Канал: {ch_title or chan_id} (ID: {chan_id}). "
        f"Мой ник: @{callback.from_user.username or ''}."
    )
    share_url = f"https://t.me/share/url?url={_urlparse.quote_plus(admin_url)}&text={_urlparse.quote_plus(text_tpl)}"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    rows = [
        [InlineKeyboardButton(text="Оформить Pro — 990 ₽/мес", url=admin_url)],
        [InlineKeyboardButton(text="Отправить заявку", url=share_url)],
        [
            InlineKeyboardButton(
                text="Что входит в Pro", callback_data="settings_subscription"
            )
        ],
        [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_SETTINGS_BACK)],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(main_text, reply_markup=kb)
    # Лог админам
    try:
        async with AsyncSessionLocal() as _slog:
            from app.repositories.admin import AdminConfigRepo as _AdminRepo
            from app.repositories.channels import ChannelsRepo as _ChRepo

            log_chat_id = await _AdminRepo(_slog).get_log_chat_id()
            if log_chat_id:
                u_link = (
                    f"https://t.me/{callback.from_user.username}"
                    if callback.from_user.username
                    else f"tg://user?id={callback.from_user.id}"
                )
                chan_link = None
                chx = await _ChRepo(_slog).get_by_id(int(chan_id))
                if chx:
                    try:
                        chat = await tg_bot.get_chat(int(chx.tg_chat_id))
                        uname = getattr(chat, "username", None)
                        if uname:
                            chan_link = f"https://t.me/{uname}"
                    except Exception:
                        chan_link = None
                extra = f" seconds={sec}" if sec is not None else ""
                txt = f"Gate hit: feature={feature} user={u_link} channel={(chan_link or chan_id)}{extra}"
                with suppress(Exception):
                    await tg_bot.send_message(
                        int(log_chat_id), txt, disable_web_page_preview=True
                    )
    except Exception:
        pass
    with suppress(TelegramBadRequest):
        await callback.answer()


async def _cb_timer_short_preset_gate(
    callback: CallbackQuery, state: FSMContext, sec: int
) -> bool:
    chan_id, is_pro, ch_title = await _get_channel_pro_info(state)
    if (not is_pro) and int(sec) < 5 * 3600:
        await _pro_gate_show_ui_and_log(
            callback,
            chan_id,
            ch_title,
            "Короткие таймеры доступны в Pro. В Free — только таймеры от 5 часов и дольше.",
            feature="autodelete_short_preset",
            sec=sec,
        )
        return True
    return False


async def _cb_repeat_pro_gate(callback: CallbackQuery, state: FSMContext) -> bool:
    chan_id, is_pro, ch_title = await _get_channel_pro_info(state)
    if not is_pro:
        await _pro_gate_show_ui_and_log(
            callback,
            chan_id,
            ch_title,
            "Автоповторы доступны в Pro. Настройте автоматический повтор публикации с любым интервалом.",
            feature="autoprepeat",
        )
        return True
    return False


@router.callback_query(F.data.startswith(CB.POST_TIMER_PRESET_PREFIX))
async def cb_post_timer_preset(callback: CallbackQuery, state: FSMContext):
    try:
        sec = int(callback.data.replace(CB.POST_TIMER_PRESET_PREFIX, ""))
    except Exception:
        return await callback.answer("Ошибка", show_alert=False)
    data = await state.get_data()
    # Гейтинг коротких таймеров вынесен в хелпер
    if await _cb_timer_short_preset_gate(callback, state, int(sec)):
        return
    payload = dict(data.get("payload") or {})
    payload["autodelete_seconds"] = int(sec)
    payload["autodelete_label"] = _format_duration_label(int(sec))
    # Отключено: не поднимаем effective до repeat+5 — удаляем строго по таймеру
    payload.pop("autodelete_views", None)
    await state.update_data(payload=payload)
    # Если пришли из карточки контент‑плана — сохраним и вернём карточку
    post_id, date_iso = await _persist_autodelete_if_cp(state)
    if post_id and date_iso:
        try:
            from app.bot.routers.content_plan import cb_cp_open_post as _open_cp

            cb2 = callback.model_copy(
                update={"data": f"cp_open_post:{post_id}:{date_iso}"}
            )  # type: ignore
            await _open_cp(cb2, state)
        except Exception:
            pass
        return await callback.answer("🗑 Таймер удаления: установлен", show_alert=True)
    # Иначе — вернёмся к меню настроек с обновлённым индикатором
    timer_set = True
    kb = settings_menu_kb(
        timer_set=timer_set,
        repeat_on=bool(data.get("repeat_on", False)),
        time_seconds=int(sec),
        views_value=int(
            (
                dict((await state.get_data()).get("payload") or {}).get(
                    "autodelete_views"
                )
                or 0
            )
        ),
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("⚙️ Настройки публикации", reply_markup=kb)
    await callback.answer("Установлено")


@router.callback_query(F.data == CB.POST_TIMER_CLEAR)
async def cb_post_timer_clear(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    payload.pop("autodelete_seconds", None)
    payload.pop("autodelete_label", None)
    await state.update_data(payload=payload)
    post_id, date_iso = await _persist_autodelete_if_cp(state)
    if post_id and date_iso:
        try:
            from app.bot.routers.content_plan import cb_cp_open_post as _open_cp

            cb2 = callback.model_copy(
                update={"data": f"cp_open_post:{post_id}:{date_iso}"}
            )  # type: ignore
            await _open_cp(cb2, state)
        except Exception:
            pass
        return await callback.answer("🗑 Таймер удаления: очищен", show_alert=True)
    kb = settings_menu_kb(
        timer_set=False,
        repeat_on=bool(data.get("repeat_on", False)),
        time_seconds=None,
        views_value=int(
            (
                dict((await state.get_data()).get("payload") or {}).get(
                    "autodelete_views"
                )
                or 0
            )
        ),
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("⚙️ Настройки публикации", reply_markup=kb)
    await callback.answer("Очищено")


@router.callback_query(F.data == CB.POST_TIMER_REPORT)
async def cb_post_timer_report(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    payload["autodelete_report"] = not bool(payload.get("autodelete_report", False))
    await state.update_data(payload=payload)
    # Перерисуем меню
    await cb_post_timer_menu(callback, state)


# --- Предпросмотр: тумблеры (перенесено из main.py) ---


@router.callback_query(F.data == CB.POST_TOGGLE_NOTIFY)
async def cb_post_toggle_notify(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(notify_on=not bool(data.get("notify_on", True)))
    # Если на экране настроек — перерисуем меню настроек, иначе обновим предпросмотр
    if data.get("ui_submenu") == "settings":
        from app.bot.keyboards.posting import settings_menu_kb

        payload = dict(data.get("payload") or {})
        secs = int(payload.get("autodelete_seconds") or 0)
        views = int(payload.get("autodelete_views") or 0)
        st = await state.get_data()
        kb = settings_menu_kb(
            timer_set=bool(secs),
            repeat_on=bool(st.get("repeat_on", False)),
            time_seconds=secs,
            views_value=views,
            notify_on=bool(st.get("notify_on", True)),
            autosign_on=bool(st.get("autosign_on", False)),
            pin_on=bool(st.get("pin_on", False)),
            comments_on=bool(st.get("comments_on", True)),
        )
        with suppress(TelegramBadRequest):
            pid = data.get("settings_msg_id") or callback.message.message_id
            await tg_bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=int(pid),
                text="⚙️ Настройки публикации",
                reply_markup=kb,
            )
        return await callback.answer()
    from app.bot.routers.main import _edit_preview_kb  # fallback

    await _edit_preview_kb(callback, state)
    await callback.answer()


# ---- Автоповтор: меню и обработчики ----


@router.callback_query(F.data == CB.POST_SETTINGS_REPEAT)
async def cb_post_repeat_menu(callback: CallbackQuery, state: FSMContext):
    await state.get_data()
    # Гейтинг: автоповторы доступны только в Pro (вынесено в хелпер)
    if await _cb_repeat_pro_gate(callback, state):
        return
    # Текст-инструкция как на макете
    title = (
        "🔁 АВТОПОВТОР\n\n"
        "Вы можете задать конкретный интервал, с которым будет выходить пост.\n\n"
        "Для этого отправьте боту желаемый период повторения публикации.\n\n"
        "6 — отправлять каждые 6 ч\n"
        "2д 6ч — отправлять каждые 2 дня 6 ч\n"
        "14д — отправлять каждые 14 дней"
    )
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    rows: list[list[InlineKeyboardButton]] = []
    # Ряд с «нет» и часами 2,4,6
    rows.append(
        [
            InlineKeyboardButton(text="нет", callback_data=CB.POST_REPEAT_CLEAR),
            InlineKeyboardButton(
                text="2 ч", callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{2 * 3600}"
            ),
            InlineKeyboardButton(
                text="4 ч", callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{4 * 3600}"
            ),
            InlineKeyboardButton(
                text="6 ч", callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{6 * 3600}"
            ),
        ]
    )
    # Часы/дни
    rows.append(
        [
            InlineKeyboardButton(
                text="12 ч", callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{12 * 3600}"
            ),
            InlineKeyboardButton(
                text="18 ч", callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{18 * 3600}"
            ),
            InlineKeyboardButton(
                text="1 д",
                callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{1 * 24 * 3600}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="2 д",
                callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{2 * 24 * 3600}",
            ),
            InlineKeyboardButton(
                text="3 д",
                callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{3 * 24 * 3600}",
            ),
            InlineKeyboardButton(
                text="4 д",
                callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{4 * 24 * 3600}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="5 д",
                callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{5 * 24 * 3600}",
            ),
            InlineKeyboardButton(
                text="7 д",
                callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{7 * 24 * 3600}",
            ),
            InlineKeyboardButton(
                text="10 д",
                callback_data=f"{CB.POST_REPEAT_PRESET_PREFIX}{10 * 24 * 3600}",
            ),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_SETTINGS_BACK)]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(title, reply_markup=kb)
    # Пометим подменю и включим режим свободного ввода интервала
    with suppress(Exception):
        await state.update_data(
            ui_submenu="repeat", repeat_menu_msg_id=callback.message.message_id
        )
    from app.bot.fsm.states import PostFSM

    await state.set_state(PostFSM.defer_time_input)
    await state.update_data(_awaiting_repeat_free=True)
    await callback.answer()


@router.callback_query(F.data.startswith(CB.POST_REPEAT_PRESET_PREFIX))
async def cb_post_repeat_preset(callback: CallbackQuery, state: FSMContext):
    try:
        sec = int(callback.data.replace(CB.POST_REPEAT_PRESET_PREFIX, ""))
    except Exception:
        return await callback.answer("Ошибка", show_alert=False)
    data = await state.get_data()
    # Гейтинг: автоповторы только в Pro (хелпер)
    if await _cb_repeat_pro_gate(callback, state):
        return
    await state.update_data(repeat_on=True, repeat_seconds=int(sec))
    # Вернуть в настройки публикации
    try:
        from app.bot.keyboards.posting import settings_menu_kb

        payload = dict(data.get("payload") or {})
        secs = int(payload.get("autodelete_seconds") or 0)
        views = int(payload.get("autodelete_views") or 0)
        kb = settings_menu_kb(
            timer_set=bool(secs),
            repeat_on=True,
            time_seconds=secs,
            views_value=views,
            notify_on=bool(data.get("notify_on", True)),
            autosign_on=bool(data.get("autosign_on", False)),
            pin_on=bool(data.get("pin_on", False)),
            comments_on=bool(data.get("comments_on", True)),
        )
        m_id = data.get("repeat_menu_msg_id") or data.get("settings_msg_id")
        if m_id:
            with suppress(TelegramBadRequest):
                await callback.message.edit_text(
                    "⚙️ Настройки публикации", reply_markup=kb
                )
            with suppress(Exception):
                await state.update_data(
                    settings_msg_id=int(m_id), ui_submenu="settings"
                )
    except Exception:
        pass
    await callback.answer("Установлено")


@router.callback_query(F.data == CB.POST_REPEAT_CLEAR)
async def cb_post_repeat_clear(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(repeat_on=False, repeat_seconds=None)
    try:
        from app.bot.keyboards.posting import settings_menu_kb

        payload = dict(data.get("payload") or {})
        secs = int(payload.get("autodelete_seconds") or 0)
        views = int(payload.get("autodelete_views") or 0)
        kb = settings_menu_kb(
            timer_set=bool(secs),
            repeat_on=False,
            time_seconds=secs,
            views_value=views,
            notify_on=bool(data.get("notify_on", True)),
            autosign_on=bool(data.get("autosign_on", False)),
            pin_on=bool(data.get("pin_on", False)),
            comments_on=bool(data.get("comments_on", True)),
        )
        m_id = data.get("repeat_menu_msg_id") or data.get("settings_msg_id")
        if m_id:
            with suppress(TelegramBadRequest):
                await callback.message.edit_text(
                    "⚙️ Настройки публикации", reply_markup=kb
                )
            with suppress(Exception):
                await state.update_data(
                    settings_msg_id=int(m_id), ui_submenu="settings"
                )
    except Exception:
        pass
    await callback.answer("Очищено")


@router.callback_query(F.data == CB.POST_TOGGLE_AUTOSIGN)
async def cb_post_toggle_autosign(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(autosign_on=not bool(data.get("autosign_on", False)))
    if data.get("ui_submenu") == "settings":
        from app.bot.keyboards.posting import settings_menu_kb

        payload = dict(data.get("payload") or {})
        secs = int(payload.get("autodelete_seconds") or 0)
        views = int(payload.get("autodelete_views") or 0)
        st = await state.get_data()
        kb = settings_menu_kb(
            timer_set=bool(secs),
            repeat_on=bool(st.get("repeat_on", False)),
            time_seconds=secs,
            views_value=views,
            notify_on=bool(st.get("notify_on", True)),
            autosign_on=bool(st.get("autosign_on", False)),
            pin_on=bool(st.get("pin_on", False)),
            comments_on=bool(st.get("comments_on", True)),
        )
        with suppress(TelegramBadRequest):
            pid = data.get("settings_msg_id") or callback.message.message_id
            await tg_bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=int(pid),
                text="⚙️ Настройки публикации",
                reply_markup=kb,
            )
        return await callback.answer()
    from app.bot.routers.main import _edit_preview_kb

    await _edit_preview_kb(callback, state)
    await callback.answer()


@router.callback_query(F.data == CB.POST_TOGGLE_PIN)
async def cb_post_toggle_pin(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(pin_on=not bool(data.get("pin_on", False)))
    if data.get("ui_submenu") == "settings":
        from app.bot.keyboards.posting import settings_menu_kb

        payload = dict(data.get("payload") or {})
        secs = int(payload.get("autodelete_seconds") or 0)
        views = int(payload.get("autodelete_views") or 0)
        st = await state.get_data()
        kb = settings_menu_kb(
            timer_set=bool(secs),
            repeat_on=bool(st.get("repeat_on", False)),
            time_seconds=secs,
            views_value=views,
            notify_on=bool(st.get("notify_on", True)),
            autosign_on=bool(st.get("autosign_on", False)),
            pin_on=bool(st.get("pin_on", False)),
            comments_on=bool(st.get("comments_on", True)),
        )
        with suppress(TelegramBadRequest):
            pid = data.get("settings_msg_id") or callback.message.message_id
            await tg_bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=int(pid),
                text="⚙️ Настройки публикации",
                reply_markup=kb,
            )
        return await callback.answer()
    from app.bot.routers.main import _edit_preview_kb

    await _edit_preview_kb(callback, state)
    await callback.answer()


@router.callback_query(F.data == CB.POST_TOGGLE_COMMENTS)
async def cb_post_toggle_comments(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(comments_on=not bool(data.get("comments_on", True)))
    if data.get("ui_submenu") == "settings":
        from app.bot.keyboards.posting import settings_menu_kb

        payload = dict(data.get("payload") or {})
        secs = int(payload.get("autodelete_seconds") or 0)
        views = int(payload.get("autodelete_views") or 0)
        st = await state.get_data()
        kb = settings_menu_kb(
            timer_set=bool(secs),
            repeat_on=bool(st.get("repeat_on", False)),
            time_seconds=secs,
            views_value=views,
            notify_on=bool(st.get("notify_on", True)),
            autosign_on=bool(st.get("autosign_on", False)),
            pin_on=bool(st.get("pin_on", False)),
            comments_on=bool(st.get("comments_on", True)),
        )
        with suppress(TelegramBadRequest):
            pid = data.get("settings_msg_id") or callback.message.message_id
            await tg_bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=int(pid),
                text="⚙️ Настройки публикации",
                reply_markup=kb,
            )
        return await callback.answer()
    from app.bot.routers.main import _edit_preview_kb

    await _edit_preview_kb(callback, state)
    await callback.answer()


@router.message(F.text == "Редактировать пост")
async def rp_edit_post_entry(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(PostFSM.edit_pick)
    text = (
        "Перешлите пост из вашего канала, который нужно изменить.\n\n"
        "Будет отредактирован только пересланный пост."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data=CB.POST_BACK)]
        ]
    )
    await message.answer(text, reply_markup=kb)


@router.message(PostFSM.edit_pick)
async def on_edit_post_pick(message: Message, state: FSMContext):
    if await _try_handle_global_reply(message, state):
        return
    orig_chat_id, orig_msg_id = await _resolve_original_post_ref(message, tg_bot)
    if orig_chat_id is None or orig_msg_id is None:
        return await message.answer(
            "❌ Не удалось определить пост. Перешлите сам пост из канала, чтобы я смог его отредактировать."
        )
    payload = _build_payload_from_message(
        message, prefer_html_caption=True, include_entities=False
    )
    if not payload:
        return await message.answer(
            "❌ Не получилось распознать содержимое пересланного поста"
        )
    chan_id: int | None = None
    async with AsyncSessionLocal() as session:
        channels = ChannelsRepo(session)
        ch = await channels.get_by_chat_id(orig_chat_id)
        if ch:
            chan_id = int(ch.id)
    await state.set_state(PostFSM.preview)
    # Включим автоподпись по умолчанию, если задана в настройках канала
    autosign_default = False
    try:
        from app.repositories.settings import ChannelSettingsRepo as _SettingsRepo

        async with AsyncSessionLocal() as session:
            repo = _SettingsRepo(session)
            st = await repo.get_by_channel_id(chan_id or 0)
            autosign_default = bool(st and (st.autosign or "").strip())
    except Exception:
        pass
    await state.update_data(
        channel_id=chan_id or 0,
        edit_chat_id=orig_chat_id,
        edit_msg_id=orig_msg_id,
        payload=payload,
        notify_on=True,
        autosign_on=autosign_default,
        pin_on=False,
        comments_on=True,
        is_draft=False,
    )
    from app.bot.routers.main import (
        _send_preview_message,
    )  # локальный импорт, чтобы избегать циклов

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


@router.callback_query(F.data == CB.POST_ADD_BUTTON)
async def cb_post_add_button(callback: CallbackQuery, state: FSMContext):
    # Переход в режим ввода кнопок без создания новых сообщений: редактируем текущую карточку
    await state.set_state(PostFSM.buttons)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    btns = payload.get("buttons") or []
    # Сформируем инструкцию
    if btns:
        text_body = (
            "Отправьте кнопки в формате:\n\n"
            "Название - ссылка (кнопки в столбик)\n"
            "Название - ссылка | Название - ссылка (кнопки в ряд)\n\n"
            "Чтобы удалить конкретную кнопку — нажмите на неё\n"
            'Чтобы удалить все кнопки сразу — нажмите "Удалить все"'
        )
    else:
        text_body = (
            "Отправьте кнопки в формате:\n\n"
            "Название - ссылка (кнопки в столбик)\n"
            "Название - ссылка | Название - ссылка (кнопки в ряд)"
        )
    text_html = "🧩 <b>Кнопки</b>\n\n" + text_body
    # Клавиатура режима кнопок
    rows_kb = []
    if btns:
        for i, row in enumerate(btns):
            r = []
            for j, b in enumerate(row):
                r.append(
                    InlineKeyboardButton(
                        text=f"✖ {b.get('text', '')}",
                        callback_data=f"{CB.POST_BUTTON_REMOVE_PREFIX}{i}:{j}",
                    )
                )
            rows_kb.append(r)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text="Удалить все", callback_data=CB.POST_DELETE_BUTTONS
                )
            ]
        )
    rows_kb.append([InlineKeyboardButton(text="Назад", callback_data=CB.POST_BACK)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)
    with suppress(TelegramBadRequest):
        pid = (await state.get_data()).get(
            "preview_msg_id"
        ) or callback.message.message_id
        try:
            await tg_bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=int(pid),
                text=text_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb,
            )
        except TelegramBadRequest:
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_caption(
                    chat_id=callback.message.chat.id,
                    message_id=int(pid),
                    caption=text_html,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
    with suppress(Exception):
        await state.update_data(ui_submenu="buttons", buttons_prompt_ids=[])
    await callback.answer()


@router.callback_query(F.data == CB.POST_EDIT_TEXT)
async def cb_post_edit_text(callback: CallbackQuery, state: FSMContext):
    # Переходим в режим ввода нового текста/подписи
    await state.set_state(PostFSM.caption)
    # Подготовим клавиатуру режима ввода: только «Назад»
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data=CB.POST_BACK)]
        ]
    )
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    has_media = payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "video_note",
        "album",
    }
    prev_id = data.get("preview_msg_id")
    instruction = "Пришлите новый текст для поста"
    if has_media:
        # Как в "Изменить медиа": удалим предпросмотр и альбомные сообщения и пришлём приглашение отдельным сообщением
        if prev_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=prev_id
                )
        album_ids = data.get("preview_album_ids") or []
        if album_ids:
            for mid in album_ids:
                with suppress(TelegramBadRequest):
                    await tg_bot.delete_message(
                        chat_id=callback.message.chat.id, message_id=mid
                    )
            await state.update_data(preview_album_ids=[])
        prompt = await callback.message.answer(instruction, reply_markup=kb)
        await state.update_data(preview_msg_id=prompt.message_id)
    else:
        # Без медиа — редактируем текущее сообщение предпросмотра
        if prev_id:
            with suppress(TelegramBadRequest):
                if payload.get("type") in {"text", "album"}:
                    await tg_bot.edit_message_text(
                        chat_id=callback.message.chat.id,
                        message_id=prev_id,
                        text=instruction,
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                elif payload.get("type") in {"photo", "video", "animation", "audio"}:
                    await tg_bot.edit_message_caption(
                        chat_id=callback.message.chat.id,
                        message_id=prev_id,
                        caption=instruction,
                        parse_mode="Markdown",
                    )
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=prev_id,
                    reply_markup=kb,
                )
    await callback.answer()


@router.message(PostFSM.caption)
async def on_post_edit_text_input(message: Message, state: FSMContext):
    # Глобальные кнопки reply разрешены в любых режимах
    if await _try_handle_global_reply(message, state):
        return
    data = await state.get_data()
    # Если ожидаем ввод цены — пересоздаём платное медиа и карточку «Медиа»
    if data.get("_awaiting_paid_price"):
        txt = (message.text or "").strip()
        try:
            val = int(txt)
        except Exception:
            return await message.answer("❌ Введите целое число от 5 до 2500")
        if val < 5 or val > 2500:
            return await message.answer("❌ Диапазон: 5–2500 звёзд")
        payload = dict(data.get("payload") or {})
        payload["media_paid_price"] = int(val)
        payload["media_paid_on"] = True
        await state.update_data(payload=payload, _awaiting_paid_price=False)
        # Удалим старые превью/альбом
        st2 = await state.get_data()
        prev_id = st2.get("preview_msg_id") or getattr(message, "message_id", None)
        media_id = st2.get("preview_media_id")
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
        for mid in st2.get("preview_album_ids") or []:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=message.chat.id, message_id=int(mid)
                )
        await state.update_data(
            preview_media_id=None, preview_msg_id=None, preview_album_ids=[]
        )
        # Отправим новое платное медиа
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
        if paid_msg_id:
            await state.update_data(preview_media_id=int(paid_msg_id))
        # Карточка «Медиа» ниже
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
        await state.update_data(preview_msg_id=m_card.message_id, ui_submenu="media")
        await state.set_state(PostFSM.preview)
        return
    payload = dict(data.get("payload") or {})
    new_text = (message.text or message.caption or "").strip()
    if not new_text:
        return await message.answer("❌ Пустой текст")
    # Обновим payload через хелпер
    payload = _apply_new_text_to_payload(payload, message, new_text)
    await state.update_data(payload=payload)
    # Перерисуем предпросмотр при наличии
    await _resend_preview_if_present_after_text(message, state, payload, data)
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
    rows, err = _parse_buttons_text(text)
    if err:
        return await message.answer(err)
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    payload["buttons"] = rows or []
    await state.update_data(payload=payload)
    # Перерисуем предпросмотр с учётом наличия кнопок
    await _resend_preview_after_buttons(message, state, payload, data)
    # Возврат в предпросмотр и очистка возможных подсказок
    await state.set_state(PostFSM.preview)
    await state.update_data(buttons_prompt_ids=[])


@router.callback_query(F.data == CB.POST_DELETE_BUTTONS)
async def cb_post_delete_all_buttons(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    payload.pop("buttons", None)
    await state.update_data(payload=payload)
    # Если были в режиме ввода кнопок — выходим обратно в предпросмотр и чистим подсказки
    cur = await state.get_state()
    if cur == PostFSM.buttons.state:
        # Остаёмся в режиме «Кнопки», но так как кнопок больше нет — показываем инструкцию «Добавить кнопки»
        text = (
            "Отправьте кнопки в формате:\n\n"
            "Название - ссылка (кнопки в столбик)\n"
            "Название - ссылка | Название - ссылка (кнопки в ряд)"
        )
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Назад", callback_data=CB.POST_BACK)]
            ]
        )
        prev_id = data.get("preview_msg_id")
        if prev_id:
            with suppress(TelegramBadRequest):
                if payload.get("type") == "text":
                    await tg_bot.edit_message_text(
                        chat_id=callback.message.chat.id,
                        message_id=prev_id,
                        text=text,
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                elif payload.get("type") in {"photo", "video", "animation", "audio"}:
                    await tg_bot.edit_message_caption(
                        chat_id=callback.message.chat.id,
                        message_id=prev_id,
                        caption=text,
                        parse_mode="Markdown",
                    )
            await tg_bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id, message_id=prev_id, reply_markup=kb
            )
        await state.update_data(buttons_prompt_ids=[])
        # Остаёмся в PostFSM.buttons
    else:
        # Не из режима кнопок — просто обновим предпросмотрную клавиатуру
        from app.bot.routers.main import _edit_preview_kb

        await _edit_preview_kb(callback, state)
    await callback.answer("Кнопки удалены")


@router.callback_query(F.data.startswith(CB.POST_BUTTON_REMOVE_PREFIX))
async def cb_post_remove_single_button(callback: CallbackQuery, state: FSMContext):
    # Формат: post_btn_rm:{row}:{col}
    try:
        _, idxs = callback.data.split(":", 1)
        i_str, j_str = idxs.split(":")
        i = int(i_str)
        j = int(j_str)
    except Exception:
        return await callback.answer("Ошибка формата", show_alert=False)
    data = await state.get_data()
    payload = dict(data.get("payload") or {})
    # Обновим payload с удалением кнопки; вернём текущий список кнопок
    btns2 = await _remove_button_from_payload(state, payload, i, j)
    if btns2 is None:
        return await callback.answer()
    # Обновим предпросмотрную клавиатуру (надпись "Кнопки"/"Добавить кнопки")
    from app.bot.routers.main import _edit_preview_kb

    await _edit_preview_kb(callback, state)
    # Если открыто окно управления кнопками — перерисуем его
    await _buttons_mode_maybe_redraw(callback, state, payload)
    await callback.answer("Удалено")


async def _remove_button_from_payload(
    state: FSMContext, payload: dict, i: int, j: int
) -> list[list[dict]] | None:
    btns = [list(r) for r in (payload.get("buttons") or [])]
    if i < 0 or i >= len(btns) or j < 0 or j >= len(btns[i]):
        return None
    btns[i].pop(j)
    btns = [r for r in btns if r]
    if btns:
        payload["buttons"] = btns
    else:
        payload.pop("buttons", None)
    await state.update_data(payload=payload)
    return payload.get("buttons") or []


async def _buttons_mode_maybe_redraw(
    callback: CallbackQuery, state: FSMContext, payload: dict
) -> None:
    cur = await state.get_state()
    if cur != PostFSM.buttons.state:
        return
    btns2 = payload.get("buttons") or []
    if not btns2:
        text = (
            "Отправьте кнопки в формате:\n\n"
            "Название - ссылка (кнопки в столбик)\n"
            "Название - ссылка | Название - ссылка (кнопки в ряд)"
        )
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Назад", callback_data=CB.POST_BACK)]
            ]
        )
        prev_id2 = (await state.get_data()).get("preview_msg_id")
        if prev_id2:
            with suppress(TelegramBadRequest):
                if payload.get("type") == "text":
                    await tg_bot.edit_message_text(
                        chat_id=callback.message.chat.id,
                        message_id=prev_id2,
                        text=text,
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                elif payload.get("type") in {"photo", "video", "animation", "audio"}:
                    await tg_bot.edit_message_caption(
                        chat_id=callback.message.chat.id,
                        message_id=prev_id2,
                        caption=text,
                        parse_mode="Markdown",
                    )
            await tg_bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id, message_id=prev_id2, reply_markup=kb
            )
        await state.update_data(buttons_prompt_ids=[])
        return
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    remove_kb_rows = []
    for i2, row2 in enumerate(btns2):
        r = []
        for j2, b2 in enumerate(row2):
            r.append(
                InlineKeyboardButton(
                    text=f"✖ {b2.get('text', '')}",
                    callback_data=f"{CB.POST_BUTTON_REMOVE_PREFIX}{i2}:{j2}",
                )
            )
        remove_kb_rows.append(r)
    if btns2:
        remove_kb_rows.append(
            [
                InlineKeyboardButton(
                    text="Удалить все", callback_data=CB.POST_DELETE_BUTTONS
                )
            ]
        )
    remove_kb_rows.append(
        [InlineKeyboardButton(text="Назад", callback_data=CB.POST_BACK)]
    )
    kb_existing = InlineKeyboardMarkup(inline_keyboard=remove_kb_rows)
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=kb_existing)


def _apply_new_text_to_payload(payload: dict, message: Message, new_text: str) -> dict:
    pl = dict(payload)
    if pl.get("type") == "text":
        pl["text"] = new_text
        if getattr(message, "entities", None):
            pl["entities"] = [e.model_dump() for e in (message.entities or [])]
        else:
            pl.pop("entities", None)
    else:
        pl["caption"] = new_text
        if getattr(message, "caption_entities", None):
            pl["caption_entities"] = [
                e.model_dump() for e in (message.caption_entities or [])
            ]
        elif getattr(message, "entities", None):
            pl["caption_entities"] = [e.model_dump() for e in (message.entities or [])]
        else:
            pl.pop("caption_entities", None)
    return pl


async def _resend_preview_if_present_after_text(
    message: Message, state: FSMContext, payload: dict, data: dict
) -> None:
    prev_id = data.get("preview_msg_id")
    if not prev_id:
        return
    with suppress(TelegramBadRequest):
        await tg_bot.delete_message(chat_id=message.chat.id, message_id=prev_id)
    from app.bot.routers.main import _send_preview_message

    new_prev = await _send_preview_message(
        message,
        payload,
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
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


def _parse_buttons_text(text: str) -> tuple[list[list[dict]] | None, str | None]:
    text = (text or "").strip()
    if not text:
        return None, "❌ Пришлите текст с кнопками по инструкции"
    rows: list[list[dict]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.split("|")]
        row: list[dict] = []
        for cell in cells:
            parts = [p.strip() for p in cell.split("-", 1)]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return None, "❌ Неверный формат. Используйте: Название - ссылка"
            row.append({"text": parts[0], "url": parts[1]})
        if row:
            rows.append(row)
    if not rows:
        return None, "❌ Не удалось распарсить кнопки"
    return rows, None


async def _resend_preview_after_buttons(
    message: Message, state: FSMContext, payload: dict, data: dict
) -> None:
    data2 = await state.get_data()
    prev_id = data2.get("preview_msg_id")
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(chat_id=message.chat.id, message_id=prev_id)
    from app.bot.routers.main import _send_preview_message

    new_prev = await _send_preview_message(
        message,
        payload,
        notify_on=bool(data.get("notify_on", True)),
        autosign_on=bool(data.get("autosign_on", False)),
        pin_on=bool(data.get("pin_on", False)),
        comments_on=bool(data.get("comments_on", True)),
        is_draft=bool(data.get("is_draft", False)),
        edit_mode=(
            data.get("edit_chat_id") is not None
            and data.get("edit_msg_id") is not None
            and not bool(data.get("is_draft", False))
        ),
        has_buttons=True,
    )
    await state.update_data(preview_msg_id=new_prev.message_id)


@router.callback_query(F.data == CB.POST_ADD_MEDIA)
async def cb_post_add_media(callback: CallbackQuery, state: FSMContext):
    # Если режим редактирования опубликованного поста — открываем меню медиа, иначе ведём в обычный сценарий
    data = await state.get_data()
    if (
        data.get("edit_chat_id")
        and data.get("edit_msg_id")
        and not bool(data.get("is_draft", False))
    ):
        return await cb_media_menu(callback, state)
    # Если текстовый пост — показываем карточку «Превью» с тумблером/позиционированием, иначе ведём в сценарий медиа
    pl = dict(data.get("payload") or {})
    if pl.get("type") == "text":
        try:
            st = await state.get_data()
            show_on = bool(st.get("preview_show_on", False))
            show_above = bool(st.get("preview_show_above", True))
            # Если превью ещё не задано пользователем — показываем простую карточку как на макете
            kb_prev = (
                build_back_kb()
                if not show_on
                else build_preview_menu_kb(show_above=show_above, show_enabled=show_on)
            )
            prev_id = data.get("preview_msg_id") or callback.message.message_id
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_text(
                    chat_id=callback.message.chat.id,
                    message_id=int(prev_id),
                    text="🖼 <b>Превью</b>\n\nОтправьте боту ссылку для отображения превью. Медиа не поддерживается.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=int(prev_id),
                    reply_markup=kb_prev,
                )
            await state.update_data(ui_submenu="preview")
            return await callback.answer()
        except Exception:
            pass
    # Иначе прежний сценарий добавления/замены медиа
    await state.set_state(PostFSM.content)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data=CB.POST_BACK)]
        ]
    )
    dict(data.get("payload") or {})
    prev_id = data.get("preview_msg_id")
    # Всегда удаляем предпросмотр, чтобы подсказка была видимой
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_id
            )
    album_ids = data.get("preview_album_ids") or []
    if album_ids:
        for mid in album_ids:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=mid
                )
        await state.update_data(preview_album_ids=[])
    prompt = await callback.message.answer("Отправьте медиа или файл", reply_markup=kb)
    await state.update_data(
        media_prompt_id=prompt.message_id, preview_msg_id=prompt.message_id
    )
    await callback.answer()


@router.callback_query(F.data == CB.POST_NEXT)
async def cb_post_next(callback: CallbackQuery, state: FSMContext):
    # Открыть настройки публикации без удаления предпросмотра: редактируем текущую карточку
    data = await state.get_data()
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
    instr_html = "⚙️ <b>Настройки публикации</b>\n\nУстановите копирование в каналы, таймер удаления, закреп и другие параметры поста."
    with suppress(TelegramBadRequest):
        pid = data.get("preview_msg_id") or callback.message.message_id
        await tg_bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=int(pid),
            text=instr_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    with suppress(Exception):
        await state.update_data(settings_msg_id=int(pid), ui_submenu="settings")
    await callback.answer()


@router.callback_query(F.data == CB.POST_SETTINGS_BACK)
async def cb_post_settings_back(callback: CallbackQuery, state: FSMContext):
    # Вернуться из настроек: при контекстe КП — в карточку поста, иначе — переписать текущую карточку редактора без удаления
    data = await state.get_data()
    try:
        meta = data.get("cp_back_to_card") or data.get("return_to_notice") or {}
        post_id = meta.get("post_id")
        date_iso = meta.get("date")
        if post_id and date_iso:
            # удалить сообщение настроек, если есть
            sid = data.get("settings_msg_id")
            if sid:
                with suppress(TelegramBadRequest):
                    await tg_bot.delete_message(
                        chat_id=callback.message.chat.id, message_id=int(sid)
                    )
            from app.bot.routers.content_plan import cb_cp_open_post as _open_cp

            cb2 = callback.model_copy(
                update={"data": f"cp_open_post:{post_id}:{date_iso}"}
            )  # type: ignore
            await _open_cp(cb2, state)
            return await callback.answer()
    except Exception:
        pass
    # Если были в подменю настроек — вернёмся к экрану настроек публикации
    if data.get("ui_submenu") in ("forward", "autodel", "repeat"):
        kb = settings_menu_kb(
            timer_set=bool(data.get("timer_set", False)),
            repeat_on=bool(data.get("repeat_on", False)),
            time_seconds=int(
                (data.get("payload") or {}).get("autodelete_seconds") or 0
            ),
            views_value=int((data.get("payload") or {}).get("autodelete_views") or 0),
            notify_on=bool(data.get("notify_on", True)),
            autosign_on=bool(data.get("autosign_on", False)),
            pin_on=bool(data.get("pin_on", False)),
            comments_on=bool(data.get("comments_on", True)),
        )
        with suppress(TelegramBadRequest):
            await callback.message.edit_text("⚙️ Настройки публикации", reply_markup=kb)
        with suppress(Exception):
            await state.update_data(
                settings_msg_id=callback.message.message_id, ui_submenu="settings"
            )
        return await callback.answer()
    # Перепишем текущую карточку обратно в «Редактор поста» (без удаления сообщений)
    from app.bot.routers.shared import build_preview_kb as _build_preview_kb

    kb = await _build_preview_kb(data)
    instr_html = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
    with suppress(TelegramBadRequest):
        pid = (
            data.get("settings_msg_id")
            or data.get("preview_msg_id")
            or callback.message.message_id
        )
        await tg_bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=int(pid),
            text=instr_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    with suppress(Exception):
        await state.update_data(
            preview_msg_id=int(pid), settings_msg_id=None, ui_submenu=None
        )
    await state.set_state(PostFSM.preview)
    with suppress(TelegramBadRequest):
        await callback.answer()


# ---- Хелперы для упрощения cb_post_back ----
async def _cb_back_restore_from_media_submenu(
    callback: CallbackQuery, state: FSMContext
) -> bool:
    state_data_ui = await state.get_data()
    if state_data_ui.get("ui_submenu") != "media":
        return False
    prev = state_data_ui
    payload = dict(prev.get("payload") or {})
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
    with suppress(TelegramBadRequest):
        pid = (await state.get_data()).get(
            "preview_msg_id"
        ) or callback.message.message_id
        instr_html = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
        await tg_bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=int(pid),
            text=instr_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    with suppress(Exception):
        await state.update_data(ui_submenu=None)
    await state.set_state(PostFSM.preview)
    with suppress(TelegramBadRequest):
        await callback.answer()
    return True


async def _cb_back_from_preview_state(
    callback: CallbackQuery, state: FSMContext
) -> bool:
    from app.bot.routers.content_plan import cb_cp_open_post

    data_all_top = await state.get_data()
    prev_id = data_all_top.get("preview_msg_id")
    if prev_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id, message_id=int(prev_id)
            )
    prev_restore = data_all_top.get("prev_editor_restore")
    if (
        isinstance(prev_restore, dict)
        and prev_restore.get("type") == "cp_publication_card"
    ):
        try:
            from app.bot.routers.content_plan_publication import (
                cb_cp_open_publication,
            )

            publication_id = int(prev_restore.get("publication_id"))
            d = str(prev_restore.get("date"))
            cb2 = callback.model_copy(
                update={"data": f"cp_open_pub:{publication_id}:{d}"}
            )  # type: ignore
            await cb_cp_open_publication(cb2, state)
            with suppress(TelegramBadRequest):
                await callback.answer()
            with suppress(Exception):
                await state.update_data(prev_editor_restore=None)
            return True
        except Exception:
            pass
    if isinstance(prev_restore, dict) and prev_restore.get("type") == "cp_card":
        try:
            pid = int(prev_restore.get("post_id"))
            d = str(prev_restore.get("date"))
            cb2 = callback.model_copy(update={"data": f"cp_open_post:{pid}:{d}"})  # type: ignore
            await cb_cp_open_post(cb2, state)
            with suppress(TelegramBadRequest):
                await callback.answer()
            with suppress(Exception):
                await state.update_data(prev_editor_restore=None)
            return True
        except Exception:
            pass
    await state.clear()
    from app.bot.routers.start import cmd_start

    await cmd_start(callback.message)
    with suppress(TelegramBadRequest):
        await callback.answer()
    return True


async def _cb_back_from_buttons_state(
    callback: CallbackQuery, state: FSMContext
) -> bool:
    data = await state.get_data()
    prev_id = data.get("preview_msg_id") or callback.message.message_id
    kb = await _build_preview_kb(data)
    instr_html = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
    with suppress(TelegramBadRequest):
        await tg_bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=int(prev_id),
            text=instr_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    await state.update_data(buttons_prompt_ids=[], ui_submenu=None)
    await state.set_state(PostFSM.preview)
    await callback.answer()
    return True


async def _cb_back_from_content_state(
    callback: CallbackQuery, state: FSMContext
) -> bool:
    from app.bot.routers.main import _send_preview_message

    data_back = await state.get_data()
    payload_back = dict(data_back.get("payload") or {})
    prev_id_back = data_back.get("preview_msg_id")
    if prev_id_back:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_id_back
            )
    preview2 = await _send_preview_message(
        callback.message,
        payload_back,
        notify_on=bool(data_back.get("notify_on", True)),
        autosign_on=bool(data_back.get("autosign_on", False)),
        pin_on=bool(data_back.get("pin_on", False)),
        comments_on=bool(data_back.get("comments_on", True)),
        is_draft=bool(data_back.get("is_draft", False)),
        edit_mode=(
            data_back.get("edit_chat_id") is not None
            and data_back.get("edit_msg_id") is not None
            and not bool(data_back.get("is_draft", False))
        ),
        has_buttons=False,
        state=state,
    )
    await state.update_data(preview_msg_id=preview2.message_id, media_prompt_id=None)
    await state.set_state(PostFSM.preview)
    await callback.answer()
    return True


def _cb_back_is_media_menu_open(callback: CallbackQuery) -> bool:
    rm = getattr(callback.message, "reply_markup", None)
    try:
        if rm and getattr(rm, "inline_keyboard", None):
            for row in rm.inline_keyboard:
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


async def _cb_back_close_media_menu(callback: CallbackQuery, state: FSMContext) -> bool:
    prev = await state.get_data()
    kb = await _build_preview_kb(prev)
    with suppress(TelegramBadRequest):
        await tg_bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=kb,
        )
    await state.set_state(PostFSM.preview)
    with suppress(TelegramBadRequest):
        await callback.answer()
    return True


async def _cb_back_to_cp_if_needed(callback: CallbackQuery, state: FSMContext) -> bool:
    from app.bot.routers.content_plan import cb_cp_open_post

    data_all_top = await state.get_data()
    prev_restore = data_all_top.get("prev_editor_restore")
    if (
        isinstance(prev_restore, dict)
        and prev_restore.get("type") == "cp_publication_card"
    ):
        try:
            from app.bot.routers.content_plan_publication import (
                cb_cp_open_publication,
            )

            publication_id = int(prev_restore.get("publication_id"))
            d = str(prev_restore.get("date"))
            cb2 = callback.model_copy(
                update={"data": f"cp_open_pub:{publication_id}:{d}"}
            )  # type: ignore
            await cb_cp_open_publication(cb2, state)
            with suppress(TelegramBadRequest):
                await callback.answer()
            with suppress(Exception):
                await state.update_data(prev_editor_restore=None)
            return True
        except Exception:
            pass
    if isinstance(prev_restore, dict) and prev_restore.get("type") == "cp_card":
        try:
            pid = int(prev_restore.get("post_id"))
            d = str(prev_restore.get("date"))
            cb2 = callback.model_copy(update={"data": f"cp_open_post:{pid}:{d}"})  # type: ignore
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                )
            await cb_cp_open_post(cb2, state)
            with suppress(TelegramBadRequest):
                await callback.answer()
            with suppress(Exception):
                await state.update_data(prev_editor_restore=None)
            return True
        except Exception:
            pass
    return False


async def _cb_back_restore_kb_only(callback: CallbackQuery, state: FSMContext) -> None:
    prev = await state.get_data()
    prev_id = prev.get("preview_msg_id")
    if prev_id:
        kb = await _build_preview_kb(prev)
        with suppress(TelegramBadRequest):
            await tg_bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id, message_id=prev_id, reply_markup=kb
            )
    await state.set_state(PostFSM.preview)
    with suppress(TelegramBadRequest):
        await callback.answer()
    logger.debug("POST_BACK: restored preview keyboard")


@router.callback_query(F.data == CB.POST_BACK)
async def cb_post_back(callback: CallbackQuery, state: FSMContext):
    logger.debug("POST_BACK: received")
    # 1) Если были в режиме ввода подписи — вернуться к предпросмотру
    cur = await state.get_state()
    from app.bot.routers.main import _send_preview_message

    if await _back_from_caption(callback, state, _send_preview_message, tg_bot):
        return
    # 2) Если были в режиме Заменить медиа — вместо карточки вернём меню Медиа на предпросмотре
    if await _back_from_content(callback, state, _send_preview_message, tg_bot):
        return

    # 3) Если открыто подменю Медиа — вернёмся к основному меню редактора
    if await _cb_back_restore_from_media_submenu(callback, state):
        return
    # 4) На экране предпросмотра: удалить предпросмотр/вернуться в старт или карточку КП
    if cur == PostFSM.preview.state:
        if await _cb_back_from_preview_state(callback, state):
            return
    # 5) Режим ввода кнопок
    if cur == PostFSM.buttons.state:
        return await _cb_back_from_buttons_state(callback, state)
    # 6) Режим запроса нового медиа
    if cur == PostFSM.content.state:
        return await _cb_back_from_content_state(callback, state)
    # 7) Закрыть подменю «Медиа», если оно открыто на текущем сообщении
    if _cb_back_is_media_menu_open(callback):
        return await _cb_back_close_media_menu(callback, state)
    # 8) Если редактор открыт из карточки контент‑плана — вернуться в неё
    if await _cb_back_to_cp_if_needed(callback, state):
        return
    # 9) По умолчанию — восстановить клавиатуру предпросмотра
    return await _cb_back_restore_kb_only(callback, state)


@router.callback_query(F.data == CB.MEDIA_MENU)
async def cb_media_menu(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pl = dict(data.get("payload") or {})
    pos = pl.get("media_pos", "top")
    spoiler = bool(pl.get("media_spoiler", False))
    paid_on = bool(pl.get("media_paid_on", False))
    star_price = int(pl.get("media_paid_price") or 0)
    is_album = pl.get("type") == "album"
    kb = build_media_menu_kb(
        pos, spoiler, paid_on=paid_on, is_album=is_album, price=(star_price or None)
    )
    # Помечаем, что мы внутри подменю «Медиа»
    with suppress(Exception):
        await state.update_data(ui_submenu="media")
    # Не создаём новые сообщения — обновляем текст/подпись и клавиатуру у текущего предпросмотра
    try:
        prev_id = data.get("preview_msg_id") or callback.message.message_id
        # Сначала текст/подпись (HTML, чтобы жирный применился гарантированно)
        header_html = (
            "<b>Медиа</b>\n\nНастройте, как будет выглядеть медиа-файл в посте."
        )
        try:
            await tg_bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=int(prev_id),
                text=header_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_caption(
                    chat_id=callback.message.chat.id,
                    message_id=int(prev_id),
                    caption=header_html,
                    parse_mode="HTML",
                )
        # Затем клавиатуру
        with suppress(TelegramBadRequest):
            await tg_bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id,
                message_id=int(prev_id),
                reply_markup=kb,
            )
    except TelegramBadRequest:
        pass
    with suppress(TelegramBadRequest):
        await callback.answer()


@router.callback_query(F.data == CB.MEDIA_POS_TOGGLE)
async def cb_media_pos_toggle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pl = dict(data.get("payload") or {})
    pl["media_pos"] = "bottom" if pl.get("media_pos") != "bottom" else "top"
    await state.update_data(payload=pl)
    # Если платный пост — пересоздадим платное медиа с новой позицией (caption сверху/снизу)
    try:
        if (
            bool(pl.get("media_paid_on", False))
            and int(pl.get("media_paid_price") or 0) > 0
            and pl.get("type") in {"photo", "video", "album"}
        ):
            # Удалим старые превью
            prev_id = data.get("preview_msg_id") or getattr(
                callback.message, "message_id", None
            )
            media_id = data.get("preview_media_id")
            if prev_id:
                with suppress(TelegramBadRequest):
                    await tg_bot.delete_message(
                        chat_id=callback.message.chat.id, message_id=int(prev_id)
                    )
            if media_id:
                with suppress(TelegramBadRequest):
                    await tg_bot.delete_message(
                        chat_id=callback.message.chat.id, message_id=int(media_id)
                    )
            for mid in data.get("preview_album_ids") or []:
                with suppress(TelegramBadRequest):
                    await tg_bot.delete_message(
                        chat_id=callback.message.chat.id, message_id=int(mid)
                    )
            await state.update_data(preview_album_ids=[])
            # Отправим новое платное медиа
            paid_msg_id = None
            try:
                from aiogram.types import InputPaidMediaPhoto, InputPaidMediaVideo
            except Exception:
                InputPaidMediaPhoto = None  # type: ignore
                InputPaidMediaVideo = None  # type: ignore
            show_above = str(pl.get("media_pos")) == "bottom"
            if pl.get("type") == "photo" and InputPaidMediaPhoto is not None:
                pm = await tg_bot.send_paid_media(
                    chat_id=callback.message.chat.id,
                    star_count=int(pl.get("media_paid_price") or 0),
                    media=[InputPaidMediaPhoto(media=pl.get("file_id"))],
                    caption=(pl.get("caption") or None),
                    parse_mode=(None if pl.get("caption_entities") else "Markdown"),
                    caption_entities=pl.get("caption_entities"),
                    show_caption_above_media=show_above,
                )
                paid_msg_id = getattr(pm, "message_id", None)
            elif pl.get("type") == "video" and InputPaidMediaVideo is not None:
                pm = await tg_bot.send_paid_media(
                    chat_id=callback.message.chat.id,
                    star_count=int(pl.get("media_paid_price") or 0),
                    media=[InputPaidMediaVideo(media=pl.get("file_id"))],
                    caption=(pl.get("caption") or None),
                    parse_mode=(None if pl.get("caption_entities") else "Markdown"),
                    caption_entities=pl.get("caption_entities"),
                    show_caption_above_media=show_above,
                )
                paid_msg_id = getattr(pm, "message_id", None)
            elif pl.get("type") == "album":
                items = list(pl.get("items") or [])
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
                    pm = await tg_bot.send_paid_media(
                        chat_id=callback.message.chat.id,
                        star_count=int(pl.get("media_paid_price") or 0),
                        media=paid_media,
                        caption=cap_to_use,
                        parse_mode=(None if cent_to_use else "Markdown"),
                        caption_entities=cent_to_use,
                        show_caption_above_media=show_above,
                    )
                    paid_msg_id = getattr(pm, "message_id", None)
            if paid_msg_id:
                await state.update_data(preview_media_id=int(paid_msg_id))
            # Под ним карточку «Медиа»
            from app.bot.keyboards.builders import build_media_menu_kb

            kb = build_media_menu_kb(
                pl.get("media_pos", "top"),
                bool(pl.get("media_spoiler", False)),
                paid_on=True,
                is_album=(pl.get("type") == "album"),
                price=int(pl.get("media_paid_price") or 0),
            )
            header_html = (
                "<b>Медиа</b>\n\nНастройте, как будет выглядеть медиа-файл в посте."
            )
            m_card = await callback.message.answer(
                header_html, reply_markup=kb, parse_mode="HTML"
            )
            await state.update_data(
                preview_msg_id=m_card.message_id, ui_submenu="media"
            )
            await state.set_state(PostFSM.preview)
            await callback.answer()
            return
    except Exception:
        pass
    # Бесплатный пост: просто обновим предпросмотр (для фото/видео)
    prev_id = data.get("preview_msg_id") or getattr(
        callback.message, "message_id", None
    )
    await _apply_preview_media(tg_bot, callback.message.chat.id, prev_id, pl)
    # Если открыто меню «Медиа», перерисуем его на том же сообщении предпросмотра
    try:
        st = await state.get_data()
        if st.get("ui_submenu") == "media":
            from app.bot.keyboards.builders import build_media_menu_kb

            pos = pl.get("media_pos", "top")
            spoiler = bool(pl.get("media_spoiler", False))
            paid_on = bool(pl.get("media_paid_on", False))
            price = int(pl.get("media_paid_price") or 0)
            is_album = pl.get("type") == "album"
            kb = build_media_menu_kb(
                pos, spoiler, paid_on=paid_on, is_album=is_album, price=(price or None)
            )
            pid = st.get("preview_msg_id") or getattr(
                callback.message, "message_id", None
            )
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=int(pid),
                    reply_markup=kb,
                )
    except Exception:
        pass


@router.callback_query(F.data == CB.MEDIA_SPOILER_TOGGLE)
async def cb_media_spoiler_toggle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pl = dict(data.get("payload") or {})
    # Если платный пост — скрываем кнопку спойлера на уровне клавиатуры, но при приходе события игнорируем
    if (
        bool(pl.get("media_paid_on", False))
        and int(pl.get("media_paid_price") or 0) > 0
    ):
        with suppress(TelegramBadRequest):
            await callback.answer()
        return
    pl["media_spoiler"] = not bool(pl.get("media_spoiler", False))
    await state.update_data(payload=pl)
    # Мгновенно обновим предпросмотр, если это фото/видео
    prev_id = data.get("preview_msg_id") or getattr(
        callback.message, "message_id", None
    )
    await _apply_preview_media(tg_bot, callback.message.chat.id, prev_id, pl)
    # Если открыто меню «Медиа», перерисуем его на том же сообщении предпросмотра
    try:
        st = await state.get_data()
        if st.get("ui_submenu") == "media":
            from app.bot.keyboards.builders import build_media_menu_kb

            pos = pl.get("media_pos", "top")
            spoiler = bool(pl.get("media_spoiler", False))
            paid_on = bool(pl.get("media_paid_on", False))
            price = int(pl.get("media_paid_price") or 0)
            is_album = pl.get("type") == "album"
            kb = build_media_menu_kb(
                pos, spoiler, paid_on=paid_on, is_album=is_album, price=(price or None)
            )
            pid = st.get("preview_msg_id") or getattr(
                callback.message, "message_id", None
            )
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=int(pid),
                    reply_markup=kb,
                )
    except Exception:
        pass


@router.callback_query(F.data == CB.MEDIA_PAID_TOGGLE)
async def cb_media_paid_toggle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pl = dict(data.get("payload") or {})
    paid_prev = bool(pl.get("media_paid_on", False))
    pl["media_paid_on"] = not paid_prev
    # Если включаем платный пост и цена не задана — выставим минимальную 1 ⭐
    if pl["media_paid_on"] and int(pl.get("media_paid_price") or 0) < 1:
        pl["media_paid_price"] = 1
    await state.update_data(payload=pl)
    # Пересоздадим предпросмотр реальным способом (sendPaidMedia/обычное медиа)
    try:
        from app.bot.routers.main import (
            _send_preview_message,
        )  # локальный импорт, чтобы избежать циклов

        st2 = await state.get_data()
        # Удалим старый предпросмотр и альбом, если был
        # Удалим карточку и медиа, если известны их id
        prev_id = st2.get("preview_msg_id") or getattr(
            callback.message, "message_id", None
        )
        media_id = st2.get("preview_media_id")
        if prev_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=int(prev_id)
                )
        if media_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=int(media_id)
                )
        for mid in st2.get("preview_album_ids") or []:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=int(mid)
                )
        await state.update_data(preview_album_ids=[])
        # Флаги редактора
        notify_on = bool(st2.get("notify_on", True))
        autosign_on = bool(st2.get("autosign_on", False))
        pin_on = bool(st2.get("pin_on", False))
        comments_on = bool(st2.get("comments_on", True))
        is_draft = bool(st2.get("is_draft", False))
        edit_mode = (
            st2.get("edit_chat_id") is not None
            and st2.get("edit_msg_id") is not None
            and not is_draft
        )
        # Отправим новый предпросмотр
        new_prev = await _send_preview_message(
            callback.message,
            pl,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=is_draft,
            edit_mode=edit_mode,
            state=state,
        )
        await state.update_data(preview_msg_id=new_prev.message_id)
    except Exception:
        pass
    # Перерисуем меню
    pos = pl.get("media_pos", "top")
    spoiler = bool(pl.get("media_spoiler", False))
    paid_on = bool(pl.get("media_paid_on", False))
    price = int(pl.get("media_paid_price") or 0)
    is_album = pl.get("type") == "album"
    kb = build_media_menu_kb(
        pos, spoiler, paid_on=paid_on, is_album=is_album, price=(price or None)
    )
    # Перерисуем клавиатуру на том же предпросмотре
    try:
        st = await state.get_data()
        pid = st.get("preview_msg_id") or getattr(callback.message, "message_id", None)
        with suppress(TelegramBadRequest):
            await tg_bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id, message_id=int(pid), reply_markup=kb
            )
    except Exception:
        pass
    await callback.answer("Платный пост: " + ("вкл" if paid_on else "выкл"))


@router.callback_query(F.data == CB.MEDIA_PAID_PRICE)
async def cb_media_paid_price(callback: CallbackQuery, state: FSMContext):
    # Попросим цену в звёздах (5..2500)
    kb = build_back_kb()
    # Меняем текст текущей карточки предпросмотра, а не создаём новое сообщение
    try:
        st = await state.get_data()
        pid = st.get("preview_msg_id") or getattr(callback.message, "message_id", None)
        if pid:
            text_price = (
                "🪙 Цена за пост\n\nОтправьте боту стоимость поста в звёздах (1–2500)."
            )
            try:
                await tg_bot.edit_message_text(
                    chat_id=callback.message.chat.id,
                    message_id=int(pid),
                    text=text_price,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except TelegramBadRequest:
                with suppress(TelegramBadRequest):
                    await tg_bot.edit_message_caption(
                        chat_id=callback.message.chat.id,
                        message_id=int(pid),
                        caption=text_price,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
    except Exception:
        pass
    await state.set_state(
        PostFSM.caption
    )  # используем существующее состояние для текстового ввода
    await state.update_data(_awaiting_paid_price=True)
    # Фиксируем, что после ввода цены нужно вернуться в подменю «Медиа»
    with suppress(Exception):
        await state.update_data(ui_submenu="media")
    await callback.answer()


@router.callback_query(F.data == CB.MEDIA_REPLACE)
async def cb_media_replace(callback: CallbackQuery, state: FSMContext):
    # Переводим в режим запроса нового медиа (как в "Изменить медиа")
    kb = build_back_kb()
    # Удалим текущий предпросмотр, чтобы осталась только подсказка
    try:
        st = await state.get_data()
        prev_id = st.get("preview_msg_id")
        if prev_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=int(prev_id)
                )
    except Exception:
        pass
    prompt = await callback.message.answer("Отправьте новое медиа", reply_markup=kb)
    await state.update_data(
        media_prompt_id=prompt.message_id,
        preview_msg_id=prompt.message_id,
        media_replace_flow=True,
    )
    # Переключимся в состояние ввода контента, чтобы «Назад» восстанавливал предпросмотр
    await state.set_state(PostFSM.content)
    with suppress(TelegramBadRequest):
        await callback.answer()


# Сохранить текущие значения автудаления из state.payload в запись PostTask,
# если мы пришли из карточки контент‑плана (state["return_to_notice"]).
async def _persist_autodelete_if_cp(state: FSMContext) -> tuple[int | None, str | None]:
    data = await state.get_data()
    meta = data.get("return_to_notice") or {}
    post_id = meta.get("post_id")
    date_iso = meta.get("date")
    if not post_id:
        return None, None
    payload = dict(data.get("payload") or {})
    try:
        async with AsyncSessionLocal() as session:
            post = await session.get(PostTask, int(post_id))
            if post is not None:
                pl = dict(post.payload or {})
                for k in (
                    "autodelete_seconds",
                    "autodelete_label",
                    "autodelete_views",
                    "autodelete_report",
                    "autodelete_effective_seconds",
                ):
                    if k in payload:
                        pl[k] = payload[k]
                    elif (
                        k in pl
                        and k
                        in {
                            "autodelete_seconds",
                            "autodelete_label",
                            "autodelete_views",
                        }
                        and k not in payload
                    ):
                        # не стираем прочие поля здесь
                        pass
                post.payload = pl
                await session.commit()
                # Если пост уже опубликован (status=done) и задан таймер в секундах —
                # планируем удаление от текущего момента и, при необходимости, отчёт.
                try:
                    # учитывать эффективный таймер, если он был сохранён
                    sec = int(
                        pl.get("autodelete_effective_seconds")
                        or pl.get("autodelete_seconds")
                        or 0
                    )
                    ids = list(pl.get("result_ids") or [])
                    if str(getattr(post, "status", "")) == "done" and sec > 0 and ids:
                        # Разрешим tg_chat_id и владельца для отчёта
                        from app.domain.models import Channel, Client

                        ch = await session.get(Channel, int(post.channel_id))
                        chat_id = int(getattr(ch, "tg_chat_id", 0)) if ch else None
                        (
                            await session.get(Client, getattr(ch, "owner_id", 0))
                            if ch
                            else None
                        )
                        bool(pl.get("autodelete_report", False))
                        pl.get("result_link")
                        if chat_id:
                            # автоудаление выполняет планировщик; здесь ничего не планируем
                            pass
                except Exception:
                    pass
    except Exception:
        pass
    return int(post_id), str(date_iso) if date_iso else None
