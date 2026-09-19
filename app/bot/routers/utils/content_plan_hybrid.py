from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.routers.shared import offset_minutes_from_tz
from app.services.content_plan_pending_rows import PendingContentPlanRow
from app.services.content_plan_published_rows import (
    PublishedContentPlanRow,
    list_published_content_plan_rows,
)
from app.services.publication_editor import publication_open_callback


@dataclass(frozen=True, slots=True)
class TimedContentPlanButtonRow:
    scheduled_at: datetime
    buttons: list[InlineKeyboardButton]
    canonical_presentation_identity: str | None = None


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
    """Render published canonical identity without requiring compatibility transport."""
    badges: list[str] = []
    if row.autodelete_views:
        badges.append(f"👁 {_views_label(row.autodelete_views)}")
    if row.autodelete_seconds:
        badges.append(f"🗑️ {_humanize_seconds(row.autodelete_seconds)}")
    if row.repeat_enabled and row.repeat_seconds:
        badges.append(f"🔁 {_humanize_seconds(row.repeat_seconds)}")

    status = "🗑️" if row.autodeleted else "✅"
    suffix = "".join(f"  {badge}" for badge in badges)
    text = f"{_local_hm(row.scheduled_at, tz_code)} {status} {row.title[:40]}{suffix}"
    callback_identity = publication_open_callback(row.publication_id, date_iso)
    return TimedContentPlanButtonRow(
        scheduled_at=row.scheduled_at,
        buttons=[
            InlineKeyboardButton(
                text=text,
                callback_data=callback_identity,
            )
        ],
        canonical_presentation_identity=callback_identity,
    )


def pending_content_plan_button_row(
    row: PendingContentPlanRow,
    *,
    date_iso: str,
    tz_code: str | None,
) -> TimedContentPlanButtonRow:
    """Render one already-deduplicated pending authority row."""

    badges: list[str] = []
    try:
        views = int(row.runtime_options.get("autodelete_views") or 0)
    except (TypeError, ValueError, OverflowError):
        views = 0
    try:
        seconds = int(row.runtime_options.get("autodelete_seconds") or 0)
    except (TypeError, ValueError, OverflowError):
        seconds = 0
    if views > 0:
        badges.append(f"👁 {_views_label(views)}")
    if seconds > 0:
        badges.append(f"🗑️ {_humanize_seconds(seconds)}")
    if row.repeat_enabled and row.repeat_seconds:
        badges.append(f"🔁 {_humanize_seconds(row.repeat_seconds)}")

    suffix = "".join(f"  {badge}" for badge in badges)
    text = f"{_local_hm(row.scheduled_at, tz_code)} ⏳ {row.title[:40]}{suffix}"
    callback_identity = publication_open_callback(row.publication_id, date_iso)
    canonical_identity = callback_identity

    return TimedContentPlanButtonRow(
        scheduled_at=row.scheduled_at,
        buttons=[
            InlineKeyboardButton(
                text=text,
                callback_data=callback_identity,
            )
        ],
        canonical_presentation_identity=canonical_identity,
    )


async def canonical_only_published_button_rows(
    session: AsyncSession,
    *,
    channel_id: int,
    start_at: datetime,
    end_at: datetime,
    date_iso: str,
    tz_code: str | None,
) -> list[TimedContentPlanButtonRow]:
    """Load canonical published presentation rows without legacy transport authority."""
    try:
        rows = await list_published_content_plan_rows(
            session,
            channel_id=channel_id,
            start_at=start_at,
            end_at=end_at,
        )
    except Exception:
        return []

    rendered: list[TimedContentPlanButtonRow] = []
    for row in rows:
        item = canonical_published_button_row(
            row,
            date_iso=date_iso,
            tz_code=tz_code,
        )
        if item is not None:
            rendered.append(item)
    return rendered


def merge_timed_content_plan_rows(
    pending_rows: list[TimedContentPlanButtonRow],
    published_rows: list[TimedContentPlanButtonRow],
) -> list[list[InlineKeyboardButton]]:
    combined = [*pending_rows, *published_rows]
    combined.sort(key=lambda item: item.scheduled_at)
    return [item.buttons for item in combined]
