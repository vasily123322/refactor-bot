from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_execution_mode import CANONICAL_EXECUTION_MODE
from app.services.scheduling import as_utc


PendingContentPlanAuthority = Literal["canonical"]


@dataclass(frozen=True, slots=True)
class PendingContentPlanRow:
    authority: PendingContentPlanAuthority
    scheduled_at: datetime
    title: str
    publication_id: int
    runtime_options: dict[str, Any]
    repeat_enabled: bool
    repeat_seconds: int | None
    repeat_group_id: int | None

    @property
    def identity(self) -> tuple[str, int]:
        return ("publication", int(self.publication_id))


@dataclass(frozen=True, slots=True)
class PendingContentPlanPage:
    rows: tuple[PendingContentPlanRow, ...]
    total_count: int
    page: int
    total_pages: int


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _canonical_title(item: ContentItem, revision: ContentRevision) -> str:
    title = " ".join(str(item.title or "").split())
    if title:
        return title[:120]
    if isinstance(revision.document, dict):
        try:
            text = " ".join(PostDocument.from_dict(revision.document).primary_text().split())
        except Exception:
            text = ""
        if text:
            return text[:120]
    return "Без названия"


def _repeat_from_canonical(
    publication: Publication,
    schedule: ScheduleEntry,
) -> tuple[bool, int | None, int | None]:
    rule = _mapping(schedule.repeat_rule)
    enabled = rule.get("enabled") is True
    seconds = _positive_int(rule.get("seconds")) if enabled else None
    if seconds is None:
        enabled = False
    schedule_meta = _mapping(schedule.meta)
    publication_meta = _mapping(publication.meta)
    group_id = (
        _positive_int(schedule_meta.get("repeat_group_id"))
        or _positive_int(publication_meta.get("repeat_group_id"))
    )
    return enabled, seconds, group_id


async def list_pending_content_plan_rows(
    session: AsyncSession,
    *,
    channel_id: int,
    tg_user_id: int,
    start_at: datetime,
    end_at: datetime,
    show_repeats: bool = True,
) -> list[PendingContentPlanRow]:
    """Return canonical pending Content Plan rows before pagination."""

    try:
        safe_channel_id = int(channel_id)
        safe_user_id = int(tg_user_id)
        start = as_utc(start_at)
        end = as_utc(end_at)
    except (TypeError, ValueError, OverflowError):
        return []
    if safe_channel_id <= 0 or safe_user_id <= 0 or end < start:
        return []

    rows = (
        await session.execute(
            select(Publication, ScheduleEntry, ContentItem, ContentRevision)
            .join(
                ScheduleEntry,
                and_(
                    ScheduleEntry.id == Publication.schedule_entry_id,
                    ScheduleEntry.channel_id == Publication.channel_id,
                    ScheduleEntry.content_item_id == Publication.content_item_id,
                    ScheduleEntry.content_revision == Publication.content_revision,
                ),
            )
            .join(Channel, Channel.id == Publication.channel_id)
            .join(Client, Client.id == Channel.owner_id)
            .join(
                ContentItem,
                and_(
                    ContentItem.id == Publication.content_item_id,
                    ContentItem.channel_id == Publication.channel_id,
                    ContentItem.kind == "post",
                ),
            )
            .join(
                ContentRevision,
                and_(
                    ContentRevision.content_item_id == Publication.content_item_id,
                    ContentRevision.revision == Publication.content_revision,
                ),
            )
            .where(
                Publication.channel_id == safe_channel_id,
                Client.tg_user_id == safe_user_id,
                Publication.execution_mode == CANONICAL_EXECUTION_MODE,
                Publication.status == "queued",
                ScheduleEntry.status == "pending",
                ScheduleEntry.scheduled_at >= start,
                ScheduleEntry.scheduled_at <= end,
            )
            .order_by(ScheduleEntry.scheduled_at.asc(), Publication.id.asc())
        )
    ).all()

    result: list[PendingContentPlanRow] = []
    for publication, schedule, item, revision in rows:
        try:
            publication_id = int(publication.id)
            scheduled_at = as_utc(schedule.scheduled_at)
        except (TypeError, ValueError, OverflowError):
            continue
        if publication_id <= 0:
            continue
        repeat_enabled, repeat_seconds, repeat_group_id = _repeat_from_canonical(
            publication, schedule
        )
        result.append(
            PendingContentPlanRow(
                authority="canonical",
                scheduled_at=scheduled_at,
                title=_canonical_title(item, revision),
                publication_id=publication_id,
                runtime_options=_mapping(
                    _mapping(publication.meta).get("runtime_options")
                ),
                repeat_enabled=repeat_enabled,
                repeat_seconds=repeat_seconds,
                repeat_group_id=repeat_group_id,
            )
        )

    if not show_repeats:
        latest_by_group: dict[int, PendingContentPlanRow] = {}
        non_repeat: list[PendingContentPlanRow] = []
        for row in result:
            if not row.repeat_enabled or row.repeat_group_id is None:
                non_repeat.append(row)
                continue
            key = int(row.repeat_group_id)
            previous = latest_by_group.get(key)
            if previous is None or (row.scheduled_at, row.identity) > (
                previous.scheduled_at,
                previous.identity,
            ):
                latest_by_group[key] = row
        result = [*non_repeat, *latest_by_group.values()]

    result.sort(key=lambda row: (row.scheduled_at, row.publication_id))
    return result


def paginate_pending_content_plan_rows(
    rows: Sequence[PendingContentPlanRow],
    *,
    page: int,
    page_size: int = 8,
) -> PendingContentPlanPage:
    """Page an already deduplicated authority set."""

    safe_size = max(1, int(page_size))
    total_count = len(rows)
    total_pages = max(1, (total_count + safe_size - 1) // safe_size)
    safe_page = min(max(0, int(page)), total_pages - 1)
    start = safe_page * safe_size
    return PendingContentPlanPage(
        rows=tuple(rows[start : start + safe_size]),
        total_count=total_count,
        page=safe_page,
        total_pages=total_pages,
    )
