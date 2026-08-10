from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_editor import publication_open_callback


def legacy_published_notice_callback(post_task_id: int, date_iso: str) -> str:
    return f"cp_open_post:{int(post_task_id)}:{date_iso}"


async def published_notice_open_callback(
    session: AsyncSession,
    *,
    post: PostTask,
    date_iso: str,
) -> str:
    """Prefer Publication identity for new notices when linkage is already proven.

    The notification is emitted immediately after Telegram confirms the publication,
    before the compatibility scheduler has committed its final `done` state and before
    PublicationAwareScheduler performs the terminal projection. Therefore this seam
    validates canonical identity/linkage but intentionally does not require terminal
    Publication/Schedule statuses yet. Any missing/corrupt/read-failure state keeps the
    legacy callback so notification delivery itself is never blocked by migration code.
    """
    try:
        task_id = int(post.id)
        channel_id = int(post.channel_id)
    except (TypeError, ValueError, OverflowError):
        return legacy_published_notice_callback(getattr(post, "id", 0) or 0, date_iso)
    if task_id <= 0 or channel_id <= 0:
        return legacy_published_notice_callback(task_id, date_iso)

    try:
        publication_id = (
            await session.execute(
                select(Publication.id)
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
                    Publication.legacy_post_task_id == task_id,
                    Publication.channel_id == channel_id,
                )
            )
        ).scalar_one_or_none()
    except Exception:
        publication_id = None

    if publication_id is None:
        return legacy_published_notice_callback(task_id, date_iso)
    try:
        safe_publication_id = int(publication_id)
    except (TypeError, ValueError, OverflowError):
        return legacy_published_notice_callback(task_id, date_iso)
    if safe_publication_id <= 0:
        return legacy_published_notice_callback(task_id, date_iso)
    return publication_open_callback(safe_publication_id, date_iso)
