from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class PublishedContentPlanRow:
    publication_id: int
    legacy_post_task_id: int | None
    scheduled_at: datetime
    title: str
    autodeleted: bool
    autodelete_seconds: int | None
    autodelete_views: int | None
    repeat_enabled: bool
    repeat_seconds: int | None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _title(item: ContentItem, revision: ContentRevision) -> str:
    explicit = " ".join(str(item.title or "").split())
    if explicit:
        return explicit[:120]
    if isinstance(revision.document, dict):
        try:
            text = " ".join(PostDocument.from_dict(revision.document).primary_text().split())
        except Exception:
            text = ""
        if text:
            return text[:120]
    return "Без названия"


def _autodelete_state(publication: Publication) -> tuple[bool, int | None, int | None]:
    meta = _safe_mapping(publication.meta)
    runtime = _safe_mapping(meta.get(AUTODELETE_RUNTIME_META_KEY))
    options = _safe_mapping(meta.get("runtime_options"))
    deleted = runtime.get("deleted") is True
    seconds = _positive_int(
        runtime.get("effective_seconds") or options.get("autodelete_seconds")
    )
    views = _positive_int(options.get("autodelete_views"))
    return deleted, seconds, views


def _repeat_state(schedule: ScheduleEntry) -> tuple[bool, int | None]:
    rule = _safe_mapping(schedule.repeat_rule)
    enabled = rule.get("enabled") is True
    seconds = _positive_int(rule.get("seconds")) if enabled else None
    return bool(enabled and seconds), seconds


async def list_published_content_plan_rows(
    session: AsyncSession,
    *,
    channel_id: int,
    start_at: datetime,
    end_at: datetime,
) -> list[PublishedContentPlanRow]:
    """List canonical published rows without depending on legacy PostTask presence."""
    try:
        safe_channel_id = int(channel_id)
        start = as_utc(start_at)
        end = as_utc(end_at)
    except (TypeError, ValueError, OverflowError):
        return []
    if safe_channel_id <= 0 or end < start:
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
                Publication.status == "published",
                ScheduleEntry.status == "completed",
                ScheduleEntry.scheduled_at >= start,
                ScheduleEntry.scheduled_at <= end,
            )
            .order_by(ScheduleEntry.scheduled_at.asc(), Publication.id.asc())
        )
    ).all()

    result: list[PublishedContentPlanRow] = []
    for publication, schedule, item, revision in rows:
        try:
            publication_id = int(publication.id)
            legacy_id = (
                int(publication.legacy_post_task_id)
                if publication.legacy_post_task_id is not None
                else None
            )
            scheduled_at = as_utc(schedule.scheduled_at)
        except (TypeError, ValueError, OverflowError):
            continue
        deleted, seconds, views = _autodelete_state(publication)
        repeat_enabled, repeat_seconds = _repeat_state(schedule)
        result.append(
            PublishedContentPlanRow(
                publication_id=publication_id,
                legacy_post_task_id=legacy_id,
                scheduled_at=scheduled_at,
                title=_title(item, revision),
                autodeleted=deleted,
                autodelete_seconds=seconds,
                autodelete_views=views,
                repeat_enabled=repeat_enabled,
                repeat_seconds=repeat_seconds,
            )
        )
    return result
