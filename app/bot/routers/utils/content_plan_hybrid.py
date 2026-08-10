from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton

from app.bot.routers.shared import offset_minutes_from_tz
from app.services.content_plan_published_rows import PublishedContentPlanRow
from app.services.publication_editor import publication_open_callback


@dataclass(frozen=True, slots=True)
class TimedContentPlanButtonRow:
    scheduled_at: datetime
    buttons: list[InlineKeyboardButton]


def _local_hm(value: datetime, tz_code: str | None) -> str:
    try:
        local = value.astimezone(ZoneInfo(tz_code)) if tz_code else value
    except Exception:
        local = value + timedelta(minutes=offset_minutes_from_tz(tz_code))
    return local.strftime("%H:%M")


def _humanize_seconds(seconds: int) -> str:
    total_minutes = max(1, int(seconds) // 60)
    days, total_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(total_minutes, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes} мин")
    return " ".join(parts) or "<1ч"


def _views_label(value: int) -> str:
    number = int(value)
    if number >= 1000 and number % 1000 == 0:
        return f"{number // 1000}к"
    return str(number)


def canonical_published_button_row(
    row: PublishedContentPlanRow,
    *,
    date_iso: str,
    tz_code: str | None,
) -> TimedContentPlanButtonRow | None:
    """Render only the first retention-safe canonical-only published slice."""
    if row.legacy_post_task_id is not None or row.repeat_enabled:
        return None

    badge = None
    if row.autodelete_views:
        badge = f"👁 {_views_label(row.autodelete_views)}"
    elif row.autodelete_seconds:
        badge = f"🗑️ {_humanize_seconds(row.autodelete_seconds)}"

    status = "🗑️" if row.autodeleted else "✅"
    suffix = f"  {badge}" if badge else ""
    text = f"{_local_hm(row.scheduled_at, tz_code)} {status} {row.title[:40]}{suffix}"
    return TimedContentPlanButtonRow(
        scheduled_at=row.scheduled_at,
        buttons=[
            InlineKeyboardButton(
                text=text,
                callback_data=publication_open_callback(row.publication_id, date_iso),
            )
        ],
    )


def merge_timed_content_plan_rows(
    legacy_rows: list[TimedContentPlanButtonRow],
    canonical_rows: list[TimedContentPlanButtonRow],
) -> list[list[InlineKeyboardButton]]:
    combined = [*legacy_rows, *canonical_rows]
    combined.sort(key=lambda item: item.scheduled_at)
    return [item.buttons for item in combined]
