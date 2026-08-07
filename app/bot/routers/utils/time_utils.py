from datetime import datetime
import calendar as cal
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.core.callbacks import CB


def _month_name_ru(m: int) -> str:
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


def _build_calendar_kb(
    channel_id: int, focus: datetime, selected: datetime | None
) -> InlineKeyboardMarkup:
    cal.setfirstweekday(cal.MONDAY)
    year, month = focus.year, focus.month
    # Навигация по месяцам
    prev_month_year = year if month > 1 else year - 1
    prev_month = month - 1 if month > 1 else 12
    next_month_year = year if month < 12 else year + 1
    next_month = month + 1 if month < 12 else 1
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                text=f"← {_month_name_ru(prev_month)}",
                callback_data=f"cp_month_prev:{channel_id}:{prev_month_year:04d}-{prev_month:02d}-01",
            ),
            InlineKeyboardButton(
                text=f"{_month_name_ru(month)}", callback_data="cp_ignore"
            ),
            InlineKeyboardButton(
                text=f"{_month_name_ru(next_month)} →",
                callback_data=f"cp_month_next:{channel_id}:{next_month_year:04d}-{next_month:02d}-01",
            ),
        ]
    )
    # Заголовки дней недели
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    rows.append([InlineKeyboardButton(text=w, callback_data="cp_ignore") for w in wd])
    # Сетка дней
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
    # Нижняя панель
    rows.append(
        [
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"cp_calendar_back:{channel_id}:{(selected or focus).date().isoformat()}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_defer_calendar_kb(
    channel_id: int, focus: datetime, selected: datetime, *, expanded: bool
) -> InlineKeyboardMarkup:
    cal.setfirstweekday(cal.MONDAY)
    year, month = focus.year, focus.month
    rows: list[list[InlineKeyboardButton]] = []
    if expanded:
        prev_month_year = year if month > 1 else year - 1
        prev_month = month - 1 if month > 1 else 12
        next_month_year = year if month < 12 else year + 1
        next_month = month + 1 if month < 12 else 1
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"← {_month_name_ru(prev_month)}",
                    callback_data=f"defer_month_prev:{channel_id}:{prev_month_year:04d}-{prev_month:02d}-01",
                ),
                InlineKeyboardButton(
                    text=f"{_month_name_ru(month)}", callback_data="defer_ignore"
                ),
                InlineKeyboardButton(
                    text=f"{_month_name_ru(next_month)} →",
                    callback_data=f"defer_month_next:{channel_id}:{next_month_year:04d}-{next_month:02d}-01",
                ),
            ]
        )
        wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        rows.append(
            [InlineKeyboardButton(text=w, callback_data="defer_ignore") for w in wd]
        )
        for week in cal.monthcalendar(year, month):
            btns = []
            for d in week:
                if d == 0:
                    btns.append(
                        InlineKeyboardButton(text=" ", callback_data="defer_ignore")
                    )
                else:
                    day_text = f"{d}"
                    if (
                        selected.year == year
                        and selected.month == month
                        and selected.day == d
                    ):
                        day_text = f"🔶 {d}"
                    btns.append(
                        InlineKeyboardButton(
                            text=day_text,
                            callback_data=f"defer_pick_day:{channel_id}:{year:04d}-{month:02d}-{d:02d}",
                        )
                    )
            rows.append(btns)
        rows.append(
            [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_SETTINGS_BACK)]
        )
    else:
        # Свернутый режим: только кнопка «Развернуть календарь» и «Назад»
        rows.append(
            [
                InlineKeyboardButton(
                    text="↓ Развернуть календарь", callback_data="defer_expand_cal"
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_SETTINGS_BACK)]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _parse_time_hhmm(s: str) -> tuple[int, int] | None:
    # 18:30
    import re as _re

    m = _re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        # 18 30
        m = _re.match(r"^(\d{1,2})\s+(\d{2})$", s)
    if not m:
        # 1830 или 930
        m = _re.match(r"^(\d{3,4})$", s)
        if m:
            val = m.group(1)
            if len(val) == 3:
                val = "0" + val  # например 930 -> 0930
            # теперь всегда длина 4
            hh = int(val[:2])
            mm = int(val[2:])
            if 0 <= hh < 24 and 0 <= mm < 60:
                return hh, mm
            return None
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if 0 <= hh < 24 and 0 <= mm < 60:
        return hh, mm
    return None
