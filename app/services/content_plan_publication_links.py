from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_editor import publication_open_callback


async def published_publication_ids_for_legacy_tasks(
    session: AsyncSession,
    *,
    channel_id: int,
    post_task_ids: Iterable[int],
) -> dict[int, int]:
    """Return linked Publication identities safe for new content-plan callbacks.

    The historical function name is retained for call-site compatibility. Promotion now
    covers both a fully queued linked occurrence and a fully published linked occurrence;
    inconsistent state is deliberately omitted so callers keep the legacy callback.
    Canonical lookup itself remains fail-soft: a read failure returns no promoted links
    and never hides the content-plan row.
    """
    try:
        safe_channel_id = int(channel_id)
    except (TypeError, ValueError, OverflowError):
        return {}
    if safe_channel_id <= 0:
        return {}

    safe_task_ids: list[int] = []
    for raw in post_task_ids:
        try:
            task_id = int(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if task_id > 0:
            safe_task_ids.append(task_id)
    safe_task_ids = sorted(set(safe_task_ids))
    if not safe_task_ids:
        return {}

    try:
        rows = (
            await session.execute(
                select(Publication.legacy_post_task_id, Publication.id)
                .join(
                    PostTask,
                    and_(
                        PostTask.id == Publication.legacy_post_task_id,
                        PostTask.channel_id == Publication.channel_id,
                    ),
                )
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
                .where(
                    Publication.channel_id == safe_channel_id,
                    Publication.legacy_post_task_id.in_(safe_task_ids),
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
            )
        ).all()
    except Exception:
        return {}

    result: dict[int, int] = {}
    for raw_task_id, raw_publication_id in rows:
        if raw_task_id is None:
            continue
        try:
            task_id = int(raw_task_id)
            publication_id = int(raw_publication_id)
        except (TypeError, ValueError, OverflowError):
            continue
        if task_id > 0 and publication_id > 0:
            result[task_id] = publication_id
    return result


def content_plan_open_callback(
    *,
    post_task_id: int,
    date_iso: str,
    published_publication_ids: dict[int, int],
) -> str:
    """Prefer Publication identity only after canonical linkage was proven."""
    task_id = int(post_task_id)
    publication_id = published_publication_ids.get(task_id)
    if publication_id is not None:
        return publication_open_callback(int(publication_id), str(date_iso))
    return f"cp_open_post:{task_id}:{date_iso}"
