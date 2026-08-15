from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry


@dataclass(frozen=True, slots=True)
class LinkedContentPlanPublication:
    publication_id: int
    legacy_post_task_id: int


async def list_linked_content_plan_publications(
    session: AsyncSession,
    *,
    channel_id: int,
    start_at: datetime,
    end_at: datetime,
) -> list[LinkedContentPlanPublication]:
    """Load canonical linked occurrences without looking Publication up by PostTask id.

    New content-plan producers start from Publication/ScheduleEntry identity for the
    requested channel/time window. ``legacy_post_task_id`` is returned only so the
    renderer can suppress the duplicate compatibility transport row while that row still
    exists. Historical/unlinked PostTask rows are deliberately absent from this result
    and remain eligible for the legacy callback fallback.
    """
    try:
        safe_channel_id = int(channel_id)
    except (TypeError, ValueError, OverflowError):
        return []
    if safe_channel_id <= 0:
        return []

    try:
        rows = (
            await session.execute(
                select(Publication.id, Publication.legacy_post_task_id)
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
                    ),
                )
                .join(
                    ContentRevision,
                    and_(
                        ContentRevision.content_item_id == Publication.content_item_id,
                        ContentRevision.revision == Publication.content_revision,
                    ),
                )
                .join(
                    PostTask,
                    and_(
                        PostTask.id == Publication.legacy_post_task_id,
                        PostTask.channel_id == Publication.channel_id,
                    ),
                )
                .where(
                    Publication.channel_id == safe_channel_id,
                    Publication.legacy_post_task_id.is_not(None),
                    ScheduleEntry.scheduled_at >= start_at,
                    ScheduleEntry.scheduled_at <= end_at,
                    or_(
                        and_(
                            Publication.status == "queued",
                            ScheduleEntry.status == "pending",
                            PostTask.status == "pending",
                        ),
                        and_(
                            Publication.status == "published",
                            ScheduleEntry.status == "completed",
                            PostTask.status == "done",
                        ),
                    ),
                )
                .order_by(ScheduleEntry.scheduled_at.asc(), Publication.id.asc())
            )
        ).all()
    except Exception:
        return []

    result: list[LinkedContentPlanPublication] = []
    for raw_publication_id, raw_task_id in rows:
        if raw_task_id is None:
            continue
        try:
            publication_id = int(raw_publication_id)
            task_id = int(raw_task_id)
        except (TypeError, ValueError, OverflowError):
            continue
        if publication_id > 0 and task_id > 0:
            result.append(
                LinkedContentPlanPublication(
                    publication_id=publication_id,
                    legacy_post_task_id=task_id,
                )
            )
    return result


def legacy_content_plan_open_callback(*, post_task_id: int, date_iso: str) -> str:
    """Compatibility callback for historical/unlinked PostTask content-plan rows."""
    task_id = int(post_task_id)
    return f"cp_open_post:{task_id}:{date_iso}"
