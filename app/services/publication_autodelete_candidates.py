from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteCandidateBatch:
    publication_ids: tuple[int, ...]
    next_cursor: int
    done: bool


class PublicationAutodeleteCandidateSelector:
    """Return a bounded canonical-only published window for later lease acquisition.

    This selector intentionally applies only structural/lifecycle predicates that are
    cheap and dialect-stable. Time-only autodelete eligibility, due time, message IDs,
    repeat/report/views semantics and all mutable runtime facts remain authoritative in
    `PublicationAutodeleteService.delete_if_due()` after a worker owns the lease.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def select_batch(
        self,
        *,
        after_publication_id: int = 0,
        limit: int = 50,
    ) -> PublicationAutodeleteCandidateBatch:
        try:
            cursor = max(0, int(after_publication_id))
        except (TypeError, ValueError, OverflowError):
            cursor = 0
        bounded_limit = max(1, min(int(limit), 200))

        rows = (
            await self.session.execute(
                select(Publication.id)
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                        ScheduleEntry.status == "completed",
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
                    Publication.id > cursor,
                    Publication.status == "published",
                )
                .order_by(Publication.id.asc())
                .limit(bounded_limit)
            )
        ).scalars().all()

        publication_ids = tuple(int(value) for value in rows)
        next_cursor = publication_ids[-1] if publication_ids else cursor
        return PublicationAutodeleteCandidateBatch(
            publication_ids=publication_ids,
            next_cursor=next_cursor,
            done=len(publication_ids) < bounded_limit,
        )
