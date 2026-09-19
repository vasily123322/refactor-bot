from __future__ import annotations
from contextlib import suppress
from datetime import datetime, timezone
import html
import calendar as cal
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from app.core.db import AsyncSessionLocal
from sqlalchemy import func, select


def month_name_ru(m: int) -> str:
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


def build_calendar_kb(
    channel_id: int, focus: datetime, selected: datetime | None
) -> InlineKeyboardMarkup:
    cal.setfirstweekday(cal.MONDAY)
    year, month = focus.year, focus.month
    prev_month_year = year if month > 1 else year - 1
    prev_month = month - 1 if month > 1 else 12
    next_month_year = year if month < 12 else year + 1
    next_month = month + 1 if month < 12 else 1
    rows = []
    rows.append(
        [
            InlineKeyboardButton(
                text=f"← {month_name_ru(prev_month)}",
                callback_data=f"cp_month_prev:{channel_id}:{prev_month_year:04d}-{prev_month:02d}-01",
            ),
            InlineKeyboardButton(
                text=f"{month_name_ru(month)}", callback_data="cp_ignore"
            ),
            InlineKeyboardButton(
                text=f"{month_name_ru(next_month)} →",
                callback_data=f"cp_month_next:{channel_id}:{next_month_year:04d}-{next_month:02d}-01",
            ),
        ]
    )
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    rows.append([InlineKeyboardButton(text=w, callback_data="cp_ignore") for w in wd])
    for week in cal.monthcalendar(year, month):
        btns = []
        for d in week:
            if d == 0:
                btns.append(InlineKeyboardButton(text=" ", callback_data="cp_ignore"))
            else:
                day_text = f"{d}"
                if (
                    selected
                    and selected.year == year
                    and selected.month == month
                    and selected.day == d
                ):
                    day_text = f"🔶 {d}"
                btns.append(
                    InlineKeyboardButton(
                        text=day_text,
                        callback_data=f"cp_pick_day:{channel_id}:{year:04d}-{month:02d}-{d:02d}",
                    )
                )
        rows.append(btns)
    rows.append(
        [
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"cp_calendar_back:{channel_id}:{(selected or focus).date().isoformat()}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_calendar(
    callback: CallbackQuery,
    channel_id: int,
    focus_date: datetime,
    selected_date: datetime | None,
) -> None:
    from app.domain.models import Channel
    from app.domain.publishing.models import Publication, ScheduleEntry

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
            .select_from(Publication)
            .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
            .where(
                Publication.channel_id == channel_id,
                Publication.status == "queued",
                ScheduleEntry.status == "pending",
                ScheduleEntry.scheduled_at >= start,
                ScheduleEntry.scheduled_at <= end,
            )
        )
        count = int(res.scalar() or 0)
        ch = await session.get(Channel, channel_id)
        ch_title = ch.title or str(ch.tg_chat_id)
    try:
        from app.bot.routers.main import tg_bot

        chat_info = await tg_bot.get_chat(ch.tg_chat_id)
        uname = getattr(chat_info, "username", None)
        if uname:
            title_link = f'<a href="https://t.me/{html.escape(uname)}">{html.escape(ch_title)}</a>'
        else:
            title_link = html.escape(ch_title)
    except Exception:
        title_link = html.escape(ch_title)
    cur = selected_date or focus_date
    text = f"На {cur.day} {month_name_ru(cur.month)} {cur.year} в канале {title_link} запланировано {count} постов."
    kb = build_calendar_kb(channel_id, focus_date, selected_date or focus_date)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
