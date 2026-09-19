from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import html
from sqlalchemy import select
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo
from app.repositories.channels import ChannelsRepo
from app.bot.bot_instance import bot as tg_bot
from app.core.callbacks import CB
from app.bot.fsm.states import PostFSM
from app.bot.routers.shared_plan import (
    build_calendar_kb as _build_calendar_kb_local,
    render_calendar as _render_calendar_local,
)
from app.bot.keyboards.posting import settings_menu_kb
from app.bot.keyboards.pagination import paginate, page_nav_row
from app.bot.routers.shared import offset_minutes_from_tz as _offset_minutes_from_tz


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


# --- Хелперы бейджей автоудаления ---
def _humanize_seconds(secs: int) -> str:
    if not secs or secs <= 0:
        return "нет"
    total_minutes = secs // 60
    days = total_minutes // (24 * 60)
    rem = total_minutes % (24 * 60)
    hours = rem // 60
    minutes = rem % 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes} мин")
    return " ".join(parts) or "<1ч"


def _views_label(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return ""
    return f"{n // 1000}к" if (n >= 1000 and n % 1000 == 0) else str(n)


def _autodel_badge(pl: dict) -> str | None:
    try:
        v = int(pl.get("autodelete_views") or 0)
        s = int(pl.get("autodelete_seconds") or 0)
    except Exception:
        v = s = 0
    if v > 0:
        return f"👁 {_views_label(v)}"
    if s > 0:
        return f"🗑️ {_humanize_seconds(s)}"
    return None


async def _render_cp_channels_list(message_or_cb, user_id: int):
    text = "Выбери канал/чат, чтобы управлять постами."
    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        channels = ChannelsRepo(session)
        client = await clients.create_or_get(user_id, None, None)
        items = await channels.list_by_owner(client.id)
    if not items:
        return await message_or_cb.answer("У вас нет добавленных каналов/чатов")
    rows = []
    for ch in items:
        # Попробуем сделать кликабельным название: username или пригласительная ссылка в карточках
        label = (ch.title or str(ch.tg_chat_id))[:40]
        try:
            chat_info = await tg_bot.get_chat(ch.tg_chat_id)
            uname = getattr(chat_info, "username", None)
            if uname:
                label = label  # подпись остаётся, ссылка будет в карточке поста
        except Exception:
            pass
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"cp_pick_channel_{ch.id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="← Главное меню", callback_data=CB.GM_GLOBAL_MENU)]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(text, reply_markup=kb)
    else:
        await message_or_cb.message.edit_text(
            text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )


@router.message(F.text == "Контент план")
async def rp_content_plan_entry(message: Message):
    await _render_cp_channels_list(message, message.from_user.id)


@router.message(F.text == "Контент-план")
async def rp_content_plan_entry_alias(message: Message):
    await rp_content_plan_entry(message)


def _format_date_human(dt: datetime) -> str:
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
    weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    wd = weekdays[dt.weekday()]
    return f"{wd}, {dt.day} {months[dt.month - 1]} {dt.year}"


async def _render_content_plan(
    callback: CallbackQuery, state: FSMContext, channel_id: int, center_date: datetime
) -> None:
    # Заголовок и количество постов на выбранную дату
    # Подсчёт запланированных постов
    from app.domain.models import Channel, Client

    async with AsyncSessionLocal() as session:
        owner_match = (
            await session.execute(
                select(Channel.id)
                .join(Client, Client.id == Channel.owner_id)
                .where(
                    Channel.id == int(channel_id),
                    Client.tg_user_id == int(callback.from_user.id),
                )
            )
        ).scalar_one_or_none()
        if owner_match is None:
            await callback.answer("Нет доступа к этому каналу", show_alert=True)
            return
        # Определим часовой пояс канала и границы суток в этом поясе
        from app.repositories.settings import ChannelSettingsRepo as _CPSettingsRepo

        tz_code = None
        try:
            repo_tz = _CPSettingsRepo(session)
            st = await repo_tz.get_by_channel_id(channel_id)
            if st and st.filters:
                tz_code = st.filters.get("tz")
        except Exception:
            pass
        # Построим границы суток в TZ канала с безопасным фолбэком
        if tz_code:
            try:
                loc = ZoneInfo(tz_code)
                start_local = datetime(
                    center_date.year,
                    center_date.month,
                    center_date.day,
                    0,
                    0,
                    0,
                    tzinfo=loc,
                )
                end_local = datetime(
                    center_date.year,
                    center_date.month,
                    center_date.day,
                    23,
                    59,
                    59,
                    999999,
                    tzinfo=loc,
                )
                start = start_local.astimezone(timezone.utc)
                end = end_local.astimezone(timezone.utc)
            except Exception:
                # Фолбэк через фиксированный сдвиг формата UTC±HH[:MM]
                off = _offset_minutes_from_tz(tz_code)
                start = datetime(
                    center_date.year,
                    center_date.month,
                    center_date.day,
                    0,
                    0,
                    0,
                    tzinfo=timezone.utc,
                ) - timedelta(minutes=off)
                end = datetime(
                    center_date.year,
                    center_date.month,
                    center_date.day,
                    23,
                    59,
                    59,
                    999999,
                    tzinfo=timezone.utc,
                ) - timedelta(minutes=off)
        else:
            # Без TZ — считаем по UTC
            start = datetime(
                center_date.year,
                center_date.month,
                center_date.day,
                0,
                0,
                0,
                tzinfo=timezone.utc,
            )
            end = datetime(
                center_date.year,
                center_date.month,
                center_date.day,
                23,
                59,
                59,
                999999,
                tzinfo=timezone.utc,
            )
        try:
            data_state = await state.get_data()
            show_repeats = bool(data_state.get("cp_show_repeats", False))
        except Exception:
            show_repeats = False

        from app.services.content_plan_pending_rows import list_pending_content_plan_rows

        pending_rows = await list_pending_content_plan_rows(
            session,
            channel_id=int(channel_id),
            tg_user_id=int(callback.from_user.id),
            start_at=start,
            end_at=end,
            show_repeats=show_repeats,
        )
        count = len(pending_rows)
        ch = await session.get(Channel, channel_id)
        ch_title = ch.title or str(ch.tg_chat_id)
    # Текст с кликабельным названием канала (username или пригласительная ссылка)
    try:
        chat_info = await tg_bot.get_chat(ch.tg_chat_id)
        uname = getattr(chat_info, "username", None)
        title_plain = ch_title
        if uname:
            title_link = f'<a href="https://t.me/{html.escape(uname)}">{html.escape(title_plain)}</a>'
        else:
            invite_url = None
            with suppress(Exception):
                inv = await tg_bot.create_chat_invite_link(
                    chat_id=int(ch.tg_chat_id),
                    name="content-plan",
                    creates_join_request=False,
                )
                invite_url = getattr(inv, "invite_link", None)
            title_link = (
                f'<a href="{html.escape(invite_url)}">{html.escape(title_plain)}</a>'
                if invite_url
                else html.escape(title_plain)
            )
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
    text = f"На {center_date.day} {months[center_date.month - 1]} {center_date.year} в канале {title_link} запланировано {count} постов."
    # Pending authority has already been deduplicated before this presentation step.
    post_rows = []
    try:
        from app.bot.routers.utils.content_plan_hybrid import (
            canonical_only_published_button_rows,
            merge_timed_content_plan_rows,
            pending_content_plan_button_row,
        )

        pending_timed_rows = [
            pending_content_plan_button_row(
                row,
                date_iso=center_date.date().isoformat(),
                tz_code=tz_code,
            )
            for row in pending_rows
        ]
        async with AsyncSessionLocal() as session:
            canonical_published_rows = await canonical_only_published_button_rows(
                session,
                channel_id=int(channel_id),
                start_at=start,
                end_at=end,
                date_iso=center_date.date().isoformat(),
                tz_code=tz_code,
            )
        post_rows = merge_timed_content_plan_rows(
            pending_timed_rows,
            canonical_published_rows,
        )
    except Exception:
        pass
    # Compact mobile browser: keep a bounded keyboard and page through the day.
    try:
        paging_state = await state.get_data()
        requested_page = int(paging_state.get("cp_page", 0) or 0)
    except Exception:
        requested_page = 0
    post_rows, safe_page, total_pages = paginate(
        post_rows, requested_page, page_size=8
    )
    if safe_page != requested_page:
        await state.update_data(cp_page=safe_page)
    page_nav = page_nav_row(
        prefix="cp_page",
        page=safe_page,
        total_pages=total_pages,
        noop_callback="cp_day_center_nop",
    )
    # Кнопки навигации по датам
    prev_day = center_date - timedelta(days=1)
    next_day = center_date + timedelta(days=1)
    left = InlineKeyboardButton(
        text=_format_date_human(prev_day),
        callback_data=f"cp_day_prev:{channel_id}:{prev_day.date().isoformat()}",
    )
    center_btn = InlineKeyboardButton(
        text=_format_date_human(center_date), callback_data="cp_day_center_nop"
    )
    right = InlineKeyboardButton(
        text=_format_date_human(next_day),
        callback_data=f"cp_day_next:{channel_id}:{next_day.date().isoformat()}",
    )
    open_cal = InlineKeyboardButton(
        text="Календарь",
        callback_data=f"cp_open_cal:{channel_id}:{center_date.date().isoformat()}",
    )
    back = InlineKeyboardButton(text="Назад", callback_data="cp_back_channels")
    # Тумблер отображения повторов: показывать все или схлопывать серии
    # Вычислим текущую настройку
    try:
        data_state2 = await state.get_data()
        show_repeats_btn = bool(data_state2.get("cp_show_repeats", False))
    except Exception:
        show_repeats_btn = False
    btn_label = "🔁 Скрыть повторы" if show_repeats_btn else "🔁 Показать повторы"
    btn_toggle = InlineKeyboardButton(
        text=btn_label, callback_data=CB.CP_TOGGLE_REPEATS
    )
    new_post = InlineKeyboardButton(
        text="✍️ Новый пост", callback_data=f"{CB.POST_PICK_CH_PREFIX}{channel_id}"
    )
    home = InlineKeyboardButton(text="🏠 Главное меню", callback_data=CB.GM_GLOBAL_MENU)
    rows_all = [[new_post], [open_cal], [btn_toggle]]
    if post_rows:
        rows_all.extend(post_rows)
    if page_nav:
        rows_all.append(page_nav)
    rows_all.extend([[left, center_btn, right], [back, home]])
    kb = InlineKeyboardMarkup(inline_keyboard=rows_all)
    try:
        await callback.message.edit_text(
            text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data == CB.CP_TOGGLE_REPEATS)
async def cb_cp_toggle_repeats(callback: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        show = bool(data.get("cp_show_repeats", False))
        await state.update_data(cp_show_repeats=(not show), cp_page=0)
        # Перерисуем текущий день
        from datetime import datetime as _dt

        cid = int(data.get("cp_channel_id") or 0)
        center_iso = data.get("cp_center")
        center = _dt.fromisoformat(center_iso) if center_iso else _dt.now(timezone.utc)
        await _render_content_plan(callback, state, cid, center)
        with suppress(TelegramBadRequest):
            await callback.answer("Обновлено")
    except Exception:
        with suppress(TelegramBadRequest):
            await callback.answer()


@router.callback_query(F.data.startswith(f"{CB.CP_REPEAT_OFF}:"))
async def cb_cp_repeat_off(callback: CallbackQuery, state: FSMContext):
    await callback.answer(
        "Эта кнопка устарела. Переоткройте контент-план.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("cp_pick_channel_"))
async def cb_cp_pick_channel(callback: CallbackQuery, state: FSMContext):
    try:
        cid = int(callback.data.split("_")[-1])
    except Exception:
        return await callback.answer("Ошибка данных", show_alert=True)
    # Центр по текущему времени канала (учитываем его часовой пояс)
    from datetime import datetime as _dt
    from app.repositories.settings import ChannelSettingsRepo as _CPSettingsRepo

    center = _dt.now()
    try:
        async with AsyncSessionLocal() as session:
            repo_tz = _CPSettingsRepo(session)
            st = await repo_tz.get_by_channel_id(cid)
            tz_code = (st.filters or {}).get("tz") if (st and st.filters) else None
            now_utc = _dt.now(timezone.utc)
            if tz_code:
                with suppress(Exception):
                    center = now_utc.astimezone(ZoneInfo(tz_code))
                if center.tzinfo is None:
                    # Fallback через фиксированный сдвиг
                    off = _offset_minutes_from_tz(tz_code)
                    center = now_utc + timedelta(minutes=off)
            else:
                # Без заданного TZ оставим текущее UTC как центр
                center = now_utc
    except Exception:
        pass
    await state.update_data(cp_channel_id=cid, cp_center=center.date().isoformat(), cp_page=0)
    await _render_content_plan(callback, state, cid, center)
    await callback.answer()


@router.callback_query(
    F.data.startswith("cp_day_prev") | F.data.startswith("cp_day_next")
)
async def cb_cp_day_shift(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    action, cid_str, date_iso = parts
    from datetime import datetime as _dt

    cid = int(cid_str)
    # Парсим выбранную дату как локальную дату канала
    from app.repositories.settings import ChannelSettingsRepo as _CPSettingsRepo

    center = _dt.fromisoformat(date_iso)
    try:
        async with AsyncSessionLocal() as session:
            repo_tz = _CPSettingsRepo(session)
            st = await repo_tz.get_by_channel_id(cid)
            tz_code = (st.filters or {}).get("tz") if (st and st.filters) else None
            if tz_code:
                with suppress(Exception):
                    center = center.replace(tzinfo=ZoneInfo(tz_code))
    except Exception:
        pass
    await state.update_data(cp_channel_id=cid, cp_center=center.date().isoformat(), cp_page=0)
    await _render_content_plan(callback, state, cid, center)
    with suppress(TelegramBadRequest):
        await callback.answer()


@router.callback_query(F.data == "cp_day_center_nop")
async def cb_cp_day_center_nop(callback: CallbackQuery):
    # Ничего не делаем, просто закрываем спиннер
    await callback.answer()


@router.callback_query(F.data.startswith("cp_page:"))
async def cb_cp_page(callback: CallbackQuery, state: FSMContext):
    try:
        page = max(0, int(callback.data.split(":", 1)[1]))
        data = await state.get_data()
        cid = int(data.get("cp_channel_id") or 0)
        center_iso = data.get("cp_center")
        if not cid or not center_iso:
            return await callback.answer("Откройте контент-план заново", show_alert=True)
        center = datetime.fromisoformat(str(center_iso))
    except Exception:
        return await callback.answer("Ошибка страницы", show_alert=False)
    await state.update_data(cp_page=page)
    await _render_content_plan(callback, state, cid, center)
    await callback.answer()


@router.callback_query(F.data == CB.EDIT_AUTODEL)
async def cb_cp_edit_autodel(callback: CallbackQuery, state: FSMContext):
    # Открыть ту же клавиатуру, что и при создании поста: в зависимости от активного режима
    try:
        # Используем сохранённый в state последний payload редактора, если есть
        data = await state.get_data()
        payload = dict((data.get("payload") or {}))
    except Exception:
        payload = {}
    # Проставим маркер возврата в карточку
    try:
        rn = (await state.get_data()).get("return_to_notice")
        if rn:
            await state.update_data(cp_back_to_card=rn)
    except Exception:
        pass
    # Если установлен режим по просмотрам — открыть меню просмотров, иначе таймера (ленивый импорт)
    if int(payload.get("autodelete_views") or 0) > 0:
        from app.bot.routers.post_editor import cb_post_view_menu as _open

        cb2 = callback.model_copy(update={"data": CB.POST_VIEW_MENU})  # type: ignore
        await _open(cb2, state)
    else:
        from app.bot.routers.post_editor import cb_post_timer_menu as _open

        cb2 = callback.model_copy(update={"data": CB.POST_SETTINGS_TIMER})  # type: ignore
        await _open(cb2, state)


@router.callback_query(F.data.startswith("cp_open_post:"))
async def cb_cp_open_post(callback: CallbackQuery, state: FSMContext):
    await callback.answer(
        "Эта кнопка устарела. Переоткройте контент-план.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("cp_edit_post:"))
async def cb_cp_edit_post(callback: CallbackQuery, state: FSMContext):
    await callback.answer(
        "Эта кнопка устарела. Переоткройте контент-план.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("cp_open_cal:"))
async def cb_cp_open_calendar(callback: CallbackQuery):
    parts = callback.data.split(":")
    # cp_open_cal:cid:YYYY-MM-DD
    if len(parts) != 3:
        return await callback.answer()
    _, cid_str, date_iso = parts
    from datetime import datetime as _dt

    cid = int(cid_str)
    date = _dt.fromisoformat(date_iso)
    await _render_calendar_local(callback, cid, date, date)
    await callback.answer()


@router.callback_query(F.data.startswith("cp_calendar_back:"))
async def cb_cp_calendar_back(callback: CallbackQuery, state: FSMContext):
    # Если мы в режиме отложенной публикации — возвращаемся к настройкам публикации
    data = await state.get_data()
    if data.get("ui_submenu") == "defer":
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
            await state.update_data(ui_submenu="settings")
        return await callback.answer()
    # Иначе — старая логика возврата к сводке дат
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    _, cid_str, date_iso = parts
    from datetime import datetime as _dt

    cid = int(cid_str)
    center = _dt.fromisoformat(date_iso)
    await _render_content_plan(callback, state, cid, center)
    await callback.answer()


@router.callback_query(
    F.data.startswith("cp_month_prev:") | F.data.startswith("cp_month_next:")
)
async def cb_cp_month_shift(callback: CallbackQuery, state: FSMContext):
    # Сдвиг месяца в календаре; учитываем режим отложенной публикации
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    action, cid_str, d_iso = parts
    from datetime import datetime as _dt

    focus = _dt.fromisoformat(d_iso)
    if action.startswith("cp_month_prev"):
        year = focus.year if focus.month > 1 else focus.year - 1
        month = focus.month - 1 if focus.month > 1 else 12
        focus = _dt(year, month, 1)
    else:
        year = focus.year if focus.month < 12 else focus.year + 1
        month = focus.month + 1 if focus.month < 12 else 1
        focus = _dt(year, month, 1)
    cid = int(cid_str)
    await _render_calendar_local(callback, cid, focus, focus)
    data = await state.get_data() if "state" in locals() else {}
    if isinstance(data, dict) and data.get("ui_submenu") == "defer":
        with suppress(Exception):
            await state.update_data(defer_date=focus.date().isoformat())
    await callback.answer()


@router.callback_query(F.data.startswith("cp_pick_day:"))
async def cb_cp_pick_day(callback: CallbackQuery, state: FSMContext):
    # Выбор конкретной даты из календаря → сводка, либо, если мы в режиме отложенной публикации — запрос времени
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
        kb = _build_calendar_kb_local(int(cid_str), focus, focus)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=kb)
        # Переходим к ожиданию времени
        await state.set_state(PostFSM.defer_time_input)
        return await callback.answer()
    # Иначе — сводка контент‑плана
    from datetime import datetime as _dt

    cid = int(cid_str)
    center = _dt.fromisoformat(d_iso)
    await _render_content_plan(callback, state, cid, center)
    await callback.answer()


@router.callback_query(F.data == "cp_back_channels")
async def cb_cp_back_channels(callback: CallbackQuery):
    try:
        await _render_cp_channels_list(callback, callback.from_user.id)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            with suppress(Exception):
                await _render_cp_channels_list(callback.message, callback.from_user.id)
    await callback.answer()
