from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.routers.shared import offset_minutes_from_tz
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
    elif row.autodelete_seconds:
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


def _row_callback_identity(row: TimedContentPlanButtonRow) -> str | None:
    if row.canonical_presentation_identity is not None:
        return row.canonical_presentation_identity
    if not row.buttons:
        return None
    callback_data = row.buttons[0].callback_data
    if not isinstance(callback_data, str):
        return None
    # A linked compatibility row may carry an extra legacy repeat-off button. Its first
    # button already has exact canonical Publication identity, so the entire compatibility
    # row can disappear once the one canonical presentation row is present.
    if callback_data.startswith("cp_open_pub:"):
        return callback_data
    return callback_data if len(row.buttons) == 1 else None


def merge_timed_content_plan_rows(
    legacy_rows: list[TimedContentPlanButtonRow],
    canonical_rows: list[TimedContentPlanButtonRow],
) -> list[list[InlineKeyboardButton]]:
    canonical_identity_counts = Counter(
        row.canonical_presentation_identity
        for row in canonical_rows
        if row.canonical_presentation_identity is not None
    )
    legacy_identity_counts = Counter(
        identity
        for row in legacy_rows
        if (identity := _row_callback_identity(row)) is not None
    )

    filtered_legacy_rows: list[TimedContentPlanButtonRow] = []
    for row in legacy_rows:
        identity = _row_callback_identity(row)
        if (
            identity is not None
            and canonical_identity_counts[identity] == 1
            and legacy_identity_counts[identity] == 1
        ):
            continue
        filtered_legacy_rows.append(row)

    combined = [*filtered_legacy_rows, *canonical_rows]
    combined.sort(key=lambda item: item.scheduled_at)
    return [item.buttons for item in combined]
