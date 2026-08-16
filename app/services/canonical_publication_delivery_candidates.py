from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.publication_execution_mode import CANONICAL_EXECUTION_MODE
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryCandidate:
    publication_id: int
    scheduled_at: datetime


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryCandidateCursor:
    scheduled_at: datetime
    publication_id: int


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryCandidateBatch:
    candidates: tuple[CanonicalPublicationDeliveryCandidate, ...]
    next_cursor: CanonicalPublicationDeliveryCandidateCursor | None
    done: bool


class CanonicalPublicationDeliveryCandidateSelector:
    """Read-only selector for due canonical Publication delivery candidates.

    The coarse query is intentionally transport-independent and excludes cheap known
    terminal/execution evidence. Every returned row is then re-proven by the canonical
    delivery planner, so this selector never becomes a second source of eligibility
    truth. No ``PostTask`` identity is read or required. Persisted execution mode is the
    exact ownership discriminator; historical NULL and intentional legacy rows are not
    canonical candidates.

    Persistent workers should use :meth:`scan_page` and retain ``next_cursor`` between
    pages. That keyset cursor prevents a bounded front window containing planner-invalid
    rows from starving valid work forever. :meth:`due` is a one-page convenience only.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def scan_page(
        self,
        *,
        limit: int = 100,
        scan_limit: int = 500,
        at: datetime | None = None,
        after: CanonicalPublicationDeliveryCandidateCursor | None = None,
    ) -> CanonicalPublicationDeliveryCandidateBatch:
        current = as_utc(at or datetime.now(timezone.utc))
        try:
            bounded_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError, OverflowError):
            bounded_limit = 100
        try:
            bounded_scan_limit = max(1, min(int(scan_limit), 500))
        except (TypeError, ValueError, OverflowError):
            bounded_scan_limit = 500

        existing_attempt = exists(
            select(PublicationAttempt.id).where(
                PublicationAttempt.publication_id == Publication.id
            )
        )
        predicates = [
            Publication.execution_mode == CANONICAL_EXECUTION_MODE,
            Publication.status == "queued",
            Publication.attempt_count == 0,
            Publication.result_link.is_(None),
            Publication.last_error.is_(None),
            ScheduleEntry.status == "pending",
            ScheduleEntry.scheduled_at <= current,
            ~existing_attempt,
        ]
        if after is not None:
            cursor_time = as_utc(after.scheduled_at)
            try:
                cursor_id = max(0, int(after.publication_id))
            except (TypeError, ValueError, OverflowError):
                cursor_id = 0
            predicates.append(
                or_(
                    ScheduleEntry.scheduled_at > cursor_time,
                    and_(
                        ScheduleEntry.scheduled_at == cursor_time,
                        Publication.id > cursor_id,
                    ),
                )
            )

        rows = (
            await self.session.execute(
                select(Publication.id, ScheduleEntry.scheduled_at)
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
                .join(
                    Channel,
                    and_(
                        Channel.id == Publication.channel_id,
                        Channel.is_active.is_(True),
                    ),
                )
                .where(*predicates)
                .order_by(
                    ScheduleEntry.scheduled_at.asc(),
                    Publication.id.asc(),
                )
                .limit(bounded_scan_limit)
            )
        ).all()

        planner = CanonicalPublicationDeliveryPlanner(self.session)
        candidates: list[CanonicalPublicationDeliveryCandidate] = []
        next_cursor = after
        processed = 0
        for row in rows:
            processed += 1
            row_scheduled_at = as_utc(row.scheduled_at)
            next_cursor = CanonicalPublicationDeliveryCandidateCursor(
                scheduled_at=row_scheduled_at,
                publication_id=int(row.id),
            )
            plan = await planner.plan(int(row.id), at=current)
            if plan is None:
                continue
            candidates.append(
                CanonicalPublicationDeliveryCandidate(
                    publication_id=int(plan.publication_id),
                    scheduled_at=as_utc(plan.scheduled_at),
                )
            )
            if len(candidates) >= bounded_limit:
                break

        exhausted_rows = processed == len(rows)
        done = exhausted_rows and len(rows) < bounded_scan_limit
        return CanonicalPublicationDeliveryCandidateBatch(
            candidates=tuple(candidates),
            next_cursor=next_cursor,
            done=done,
        )

    async def due(
        self,
        *,
        limit: int = 100,
        at: datetime | None = None,
    ) -> list[CanonicalPublicationDeliveryCandidate]:
        """Return candidates from the first bounded page.

        Long-lived workers should use ``scan_page`` and advance its cursor instead of
        repeatedly calling this convenience method from the beginning of the queue.
        """

        batch = await self.scan_page(limit=limit, scan_limit=500, at=at)
        return list(batch.candidates)
