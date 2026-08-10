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
from sqlalchemy import select, func
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo
from app.repositories.channels import ChannelsRepo
from app.bot.bot_instance import bot as tg_bot
from app.core.callbacks import CB
from app.bot.fsm.states import PostFSM
from app.bot.routers.shared_plan import (
    build_calendar_kb as _build_calendar_kb,
    render_calendar as _render_calendar,
)
from app.bot.keyboards.posting import settings_menu_kb
from app.bot.keyboards.pagination import paginate, page_nav_row
from app.bot.routers.shared import escape_markdown_label as _escape_markdown_label
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
    from app.domain.models import Channel, Client, PostTask

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
        # Простой подсчёт: фильтруем pending в этот локальный день (в UTC) по channel_id
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
    # Кнопки постов за выбранную дату (и pending, и done)
    post_rows = []
    try:
        from app.bot.routers.utils.content_plan_hybrid import (
            TimedContentPlanButtonRow,
            canonical_published_button_row,
            merge_timed_content_plan_rows,
        )
        from app.services.content_plan_published_rows import (
            list_published_content_plan_rows,
        )
        from app.services.scheduling import as_utc

        timed_post_rows: list[TimedContentPlanButtonRow] = []
        from app.domain.models import PostTask

        async with AsyncSessionLocal() as session:
            # Используем те же границы суток (локальные, переведённые в UTC)
            res = await session.execute(
                select(PostTask)
                .where(
                    (PostTask.channel_id == channel_id)
                    & (PostTask.scheduled_at >= start)
                    & (PostTask.scheduled_at <= end)
                )
                .order_by(PostTask.scheduled_at.asc())
            )
            items = list(res.scalars().all())
            # Схлопнём повторы: оставим последнюю запись каждой серии, чтобы видеть свежие посты
            try:
                data_state = await state.get_data()
                show_repeats = bool(data_state.get("cp_show_repeats", False))
            except Exception:
                show_repeats = False
            if not show_repeats and items:
                group_latest: dict[int, PostTask] = {}
                non_repeat: list[PostTask] = []
                for pp in items:
                    plp = pp.payload or {}
                    gid = int(plp.get("repeat_group_id") or 0)
                    is_rep = (gid > 0) or (
                        bool(plp.get("repeat_on"))
                        and int(plp.get("repeat_seconds") or 0) > 0
                    )
                    if is_rep and gid > 0:
                        prev = group_latest.get(gid)
                        if (prev is None) or (
                            (pp.scheduled_at or datetime.min)
                            > (prev.scheduled_at or datetime.min)
                        ):
                            group_latest[gid] = pp
                    else:
                        non_repeat.append(pp)
                items = sorted(
                    non_repeat + list(group_latest.values()),
                    key=lambda x: (x.scheduled_at or datetime.min),
                )
            # Определим часовой пояс канала для локализации времени
            from app.repositories.settings import ChannelSettingsRepo as _CPSettingsRepo

            tz_code = None
            try:
                repo_tz = _CPSettingsRepo(session)
                st = await repo_tz.get_by_channel_id(channel_id)
                if st and st.filters:
                    tz_code = st.filters.get("tz")
            except Exception:
                pass
            from app.services.content_plan_publication_links import (
                content_plan_open_callback,
                published_publication_ids_for_legacy_tasks,
            )

            published_publication_ids = (
                await published_publication_ids_for_legacy_tasks(
                    session,
                    channel_id=int(channel_id),
                    post_task_ids=[int(item.id) for item in items],
                )
                if items
                else {}
            )
            for p in items:
                when = p.scheduled_at
                if when:
                    try:
                        local_when = (
                            when.astimezone(ZoneInfo(tz_code)) if tz_code else when
                        )
                    except Exception:
                        local_when = when + timedelta(
                            minutes=_offset_minutes_from_tz(tz_code)
                        )
                    hm = local_when.strftime("%H:%M")
                else:
                    hm = "--:--"
                pl = p.payload or {}
                if pl.get("type") == "text":
                    first = (pl.get("text") or "").strip().splitlines()[
                        0
                    ] or "Без названия"
                else:
                    cap = pl.get("caption") or pl.get("text") or ""
                    first = cap.strip().splitlines()[0] or "Медиа"
                # Статус: удалён → корзина, опубликован → галка, отложен → часы
                is_deleted = bool(pl.get("autodeleted"))
                if is_deleted:
                    status_emoji = "🗑️"
                elif (p.status or "").lower() == "done":
                    status_emoji = "✅"
                else:
                    status_emoji = "⏳"
                badge = _autodel_badge(pl)
                # Бейдж автоповтора: если включён, добавим 🔁 и интервал
                rep = None
                try:
                    if pl.get("repeat_on") and int(pl.get("repeat_seconds") or 0) > 0:
                        rs = int(pl.get("repeat_seconds"))
                        # Человекочитаемая метка (reuse из пост-редактора)
                        from app.bot.routers.post_editor import (
                            _format_duration_label as _lab,
                        )

                        rep = f"🔁 {_lab(rs)}"
                except Exception:
                    rep = None
                suffix = (f"  {badge}" if badge else "") + (f"  {rep}" if rep else "")
                btn_text = f"{hm} {status_emoji} {first[:40]}{suffix}"
                row_btns = [
                    InlineKeyboardButton(
                        text=btn_text,
                        callback_data=content_plan_open_callback(
                            post_task_id=int(p.id),
                            date_iso=center_date.date().isoformat(),
                            published_publication_ids=published_publication_ids,
                        ),
                    )
                ]
                # Быстрая кнопка отключить автоповтор для серии
                try:
                    if pl.get("repeat_on") and int(pl.get("repeat_seconds") or 0) > 0:
                        rg = pl.get("repeat_group_id") or p.id
                        row_btns.append(
                            InlineKeyboardButton(
                                text="⏹ Повтор выкл",
                                callback_data=f"{CB.CP_REPEAT_OFF}:{rg}",
                            )
                        )
                except Exception:
                    pass
                timed_post_rows.append(
                    TimedContentPlanButtonRow(
                        scheduled_at=as_utc(p.scheduled_at),
                        buttons=row_btns,
                    )
                )

            canonical_timed_rows: list[TimedContentPlanButtonRow] = []
            try:
                canonical_rows = await list_published_content_plan_rows(
                    session,
                    channel_id=int(channel_id),
                    start_at=start,
                    end_at=end,
                )
                date_iso = center_date.date().isoformat()
                for canonical_row in canonical_rows:
                    rendered = canonical_published_button_row(
                        canonical_row,
                        date_iso=date_iso,
                        tz_code=tz_code,
                    )
                    if rendered is not None:
                        canonical_timed_rows.append(rendered)
            except Exception:
                # Canonical listing is a migration enhancement. Read failures must
                # preserve the proven legacy PostTask list rather than blank the day.
                canonical_timed_rows = []

            post_rows = merge_timed_content_plan_rows(
                timed_post_rows,
                canonical_timed_rows,
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
    # Формат: cp_repeat_off:<repeat_group_id>
    try:
        _, rg_str = callback.data.split(":", 1)
        rg = int(rg_str)
    except Exception:
        return await callback.answer("Ошибка", show_alert=False)
    from app.domain.models import PostTask

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(PostTask).where(PostTask.status == "pending")
        )
        for p in list(res.scalars().all()):
            pl = dict(p.payload or {})
            if (pl.get("repeat_group_id") or p.id) == rg:
                pl["repeat_on"] = False
                pl.pop("repeat_seconds", None)
                p.payload = pl
        await session.commit()
    with suppress(TelegramBadRequest):
        await callback.answer("Автоповтор отключён")


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
    # Открыть карточку поста из контент‑плана
    # Формат: cp_open_post:post_id:YYYY-MM-DD
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    _, post_id_str, date_iso = parts
    try:
        post_id = int(post_id_str)
    except Exception:
        return await callback.answer("Ошибка данных", show_alert=True)
    from app.domain.models import PostTask, Channel

    async with AsyncSessionLocal() as session:
        post = await session.get(PostTask, post_id)
        if not post:
            return await callback.answer("Пост не найден", show_alert=True)
        ch = await session.get(Channel, post.channel_id)
        tg_chat_id = int(ch.tg_chat_id) if ch else None
    # Определим ссылку если пост опубликован
    link_line = "Ссылка: нет"
    if (post.status or "").lower() == "done" and tg_chat_id:
        try:
            pl = post.payload or {}
            link = pl.get("result_link")
            if not link:
                ids = pl.get("result_ids")
                mid = ids[-1] if isinstance(ids, list) and ids else None
                if mid:
                    chat = await tg_bot.get_chat(tg_chat_id)
                    uname = getattr(chat, "username", None)
                    link = (
                        f"https://t.me/{uname}/{mid}"
                        if uname
                        else f"https://t.me/c/{str(tg_chat_id)[4:]}/{mid}"
                    )
            if link:
                link_line = f"Ссылка: {link}"
        except Exception:
            pass
    # Локальное время даты
    from app.repositories.settings import ChannelSettingsRepo

    local_str = ""
    try:
        async with AsyncSessionLocal() as session:
            repo = ChannelSettingsRepo(session)
            st = await repo.get_by_channel_id(post.channel_id)
            tz_code = (st.filters or {}).get("tz") if (st and st.filters) else None
            if post.scheduled_at is not None:
                try:
                    local_dt = (
                        post.scheduled_at.astimezone(ZoneInfo(tz_code))
                        if tz_code
                        else post.scheduled_at
                    )
                except Exception:
                    local_dt = post.scheduled_at + timedelta(
                        minutes=_offset_minutes_from_tz(tz_code)
                    )
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
                wd = [
                    "понедельник",
                    "вторник",
                    "среда",
                    "четверг",
                    "пятница",
                    "суббота",
                    "воскресенье",
                ][local_dt.weekday()]
                local_str = f"{local_dt.day} {months[local_dt.month - 1]} {local_dt.year} {local_dt.strftime('%H:%M')} ({wd})"
    except Exception:
        pass
    status = (post.status or "pending").lower()
    if status == "done":
        # Добавим кликабельное имя канала
        try:
            chat_info = await tg_bot.get_chat(tg_chat_id) if tg_chat_id else None
            uname = getattr(chat_info, "username", None) if chat_info else None
            chan_title_cp = (ch.title if ch else None) or (
                str(tg_chat_id) if tg_chat_id else "канал"
            )
            if uname:
                title_link_cp = (
                    f"[{_escape_markdown_label(chan_title_cp)}](https://t.me/{uname})"
                )
            else:
                title_link_cp = _escape_markdown_label(chan_title_cp)
        except Exception:
            title_link_cp = _escape_markdown_label(
                (ch.title if ch else None)
                or (str(tg_chat_id) if tg_chat_id else "канал")
            )
        # Сформируем статус‑иконку и бейдж автоудаления
        pl_settings = post.payload or {}
        status_icon = "🗑️" if bool(pl_settings.get("autodeleted")) else "✅"
        badge = _autodel_badge(pl_settings)
        autodel_line = badge or "Таймер удаления: нет"
        text = (
            f"Статус: Опубликован {status_icon}\n"
            f"{link_line}\n"
            f"Канал: {title_link_cp}\n"
            f"Дата: {local_str}\n"
            f"{autodel_line}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Дублировать", callback_data=CB.EDIT_DUP),
                    InlineKeyboardButton(
                        text="Изменить",
                        callback_data=f"cp_edit_post:{post.id}:{date_iso}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="☑️ Это рекламный пост", callback_data=CB.EDIT_AD_TOGGLE
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=autodel_line, callback_data=CB.EDIT_AUTODEL
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=f"cp_delete_post:{post.id}:{date_iso}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="← Назад",
                        callback_data=f"cp_open_cal:{post.channel_id}:{date_iso}",
                    )
                ],
            ]
        )
    else:
        # Добавим кликабельное имя канала
        try:
            chat_info = await tg_bot.get_chat(tg_chat_id) if tg_chat_id else None
            uname = getattr(chat_info, "username", None) if chat_info else None
            chan_title_cp = (ch.title if ch else None) or (
                str(tg_chat_id) if tg_chat_id else "канал"
            )
            if uname:
                title_link_cp = (
                    f"[{_escape_markdown_label(chan_title_cp)}](https://t.me/{uname})"
                )
            else:
                title_link_cp = _escape_markdown_label(chan_title_cp)
        except Exception:
            title_link_cp = _escape_markdown_label(
                (ch.title if ch else None)
                or (str(tg_chat_id) if tg_chat_id else "канал")
            )
        pl_settings = post.payload or {}
        status_icon = "🗑️" if bool(pl_settings.get("autodeleted")) else "⏳"
        badge = _autodel_badge(pl_settings)
        autodel_line = badge or "Таймер удаления: нет"
        text = (
            f"Статус: Отложен {status_icon}\n"
            f"Ссылка: нет\n"
            f"Канал: {title_link_cp}\n"
            f"Дата: {local_str}\n"
            f"{autodel_line}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Дублировать", callback_data=CB.EDIT_DUP),
                    InlineKeyboardButton(
                        text="Изменить",
                        callback_data=f"cp_edit_post:{post.id}:{date_iso}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="☑️ Это рекламный пост", callback_data=CB.EDIT_AD_TOGGLE
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=autodel_line, callback_data=CB.EDIT_AUTODEL
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Изменить дату", callback_data=CB.POST_SETTINGS_DEFER
                    ),
                    InlineKeyboardButton(
                        text="🔥 Опубликовать", callback_data=CB.POST_SETTINGS_PUBLISH
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=f"cp_delete_post:{post.id}:{date_iso}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="← Назад",
                        callback_data=f"cp_open_cal:{post.channel_id}:{date_iso}",
                    )
                ],
            ]
        )
    # Сохраним контекст возврата в уведомление
    try:
        await state.update_data(return_to_notice={"post_id": post.id, "date": date_iso})
    except Exception:
        pass
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest:
        with suppress(TelegramBadRequest):
            await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("cp_delete_post:"))
async def cb_cp_delete_post(callback: CallbackQuery, state: FSMContext):
    # Формат: cp_delete_post:post_id:YYYY-MM-DD
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    _, post_id_str, date_iso = parts
    try:
        post_id = int(post_id_str)
    except Exception:
        return await callback.answer("Ошибка данных", show_alert=True)
    from app.domain.models import PostTask

    async with AsyncSessionLocal() as session:
        post = await session.get(PostTask, post_id)
        if not post:
            return await callback.answer("Пост не найден", show_alert=True)
        try:
            await session.delete(post)
            await session.commit()
        except Exception:
            return await callback.answer("Не удалось удалить", show_alert=True)
    # После удаления вернёмся к списку постов на ту же дату
    try:
        cid = int((await state.get_data()).get("cp_channel_id") or 0)
        if not cid:
            return await callback.answer("Удалено", show_alert=False)
        from datetime import datetime as _dt

        center = _dt.fromisoformat(date_iso)
        await _render_content_plan(callback, state, cid, center)
        with suppress(TelegramBadRequest):
            await callback.answer("🗑 Удалено", show_alert=False)
    except Exception:
        await callback.answer("🗑 Удалено", show_alert=False)


@router.callback_query(F.data.startswith("cp_edit_post:"))
async def cb_cp_edit_post(callback: CallbackQuery, state: FSMContext):
    # Формат: cp_edit_post:post_id:YYYY-MM-DD — открыть редактор для опубликованного поста
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    _, post_id_str, date_iso = parts
    try:
        post_id = int(post_id_str)
    except Exception:
        return await callback.answer("Ошибка данных", show_alert=True)
    from app.domain.models import PostTask, Channel

    async with AsyncSessionLocal() as session:
        post = await session.get(PostTask, post_id)
        if not post:
            return await callback.answer("Пост не найден", show_alert=True)
        ch = await session.get(Channel, post.channel_id)
        if not ch:
            return await callback.answer("Канал не найден", show_alert=True)
        tg_chat_id = int(ch.tg_chat_id)
        pl = dict(post.payload or {})
        # Извлечём основной id опубликованного сообщения
        mid = None
        primary = pl.get("primary_message_id")
        if primary is not None:
            try:
                mid = int(primary)
            except Exception:
                mid = None
        if mid is None:
            ids = pl.get("result_ids")
            if isinstance(ids, list) and ids:
                try:
                    mid = int(ids[-1])
                except Exception:
                    mid = None
        # Очистим служебные поля и подготовим флаги медиа
        pl.get("media_pos", "top")  # top|bottom
        bool(pl.get("media_spoiler", False))
        pl.pop("result_ids", None)
        pl.pop("result_link", None)
        pl.pop("notify_context", None)
    if mid is None:
        return await callback.answer(
            "Не удалось определить сообщение для редактирования", show_alert=True
        )
    # Сохраняем контекст редактора (не очищаем state, чтобы сохранить return_to_notice)
    await state.update_data(
        channel_id=int(post.channel_id),
        edit_chat_id=tg_chat_id,
        edit_msg_id=mid,
        payload=pl,
        notify_on=True,
        autosign_on=False,
        pin_on=False,
        comments_on=True,
        is_draft=False,
        prev_editor_restore={"type": "cp_card", "post_id": post.id, "date": date_iso},
    )
    # Перед показом предпросмотра удалим предыдущее сообщение (карточку из контент‑плана) и старый предпросмотр, если был
    try:
        prev_state = await state.get_data()
        old_prev_id = prev_state.get("preview_msg_id")
        if old_prev_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=int(old_prev_id)
                )
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id, message_id=callback.message.message_id
            )
    except Exception:
        pass
    # Покажем предпросмотр в режиме редактирования
    from app.bot.routers.main import _send_preview_message

    preview = await _send_preview_message(
        callback.message,
        pl,
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


def _month_name_ru(m: int) -> str:
    # совместимость; не используется после выноса в shared_plan
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
    return months[m - 1]


def _build_calendar_kb_local(
    channel_id: int, focus: datetime, selected: datetime | None
) -> InlineKeyboardMarkup:
    # совместимость; оставляем обёртку на общий хелпер
    return _build_calendar_kb(channel_id, focus, selected)


async def _render_calendar_local(
    callback: CallbackQuery,
    channel_id: int,
    focus_date: datetime,
    selected_date: datetime | None,
) -> None:
    # совместимость; делегируем общий хелпер
    await _render_calendar(callback, channel_id, focus_date, selected_date)


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
