from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.scheduling import as_utc


class PlannerError(RuntimeError):
    pass


class PlannerNotFoundError(PlannerError):
    pass


class PlannerConflictError(PlannerError):
    pass


@dataclass(frozen=True, slots=True)
class PlannerEntry:
    schedule_id: int
    channel_id: int
    content_item_id: int
    content_revision: int
    content_title: str | None
    content_kind: str
    scheduled_at: datetime
    timezone: str | None
    schedule_status: str
    repeat_rule: dict
    publication_id: int | None
    publication_status: str | None
    telegram_message_ids: list[int] | None
    result_link: str | None
    last_error: str | None
    attempt_number: int | None
    attempt_status: str | None
    attempt_started_at: datetime | None
    attempt_finished_at: datetime | None
    legacy_post_task_id: int | None


class PlannerService:
    """Planner read/write boundary synchronized with the legacy scheduler bridge."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _current_attempts(
        self,
        publications: Iterable[Publication | None],
    ) -> dict[int, PublicationAttempt]:
        publication_ids = [
            int(publication.id)
            for publication in publications
            if publication is not None and int(publication.attempt_count or 0) > 0
        ]
        if not publication_ids:
            return {}

        rows = list(
            (
                await self.session.execute(
                    select(PublicationAttempt)
                    .join(
                        Publication,
                        Publication.id == PublicationAttempt.publication_id,
                    )
                    .where(
                        Publication.id.in_(publication_ids),
                        PublicationAttempt.attempt == Publication.attempt_count,
                    )
                )
            ).scalars().all()
        )
        return {int(attempt.publication_id): attempt for attempt in rows}

    @staticmethod
    def _entry(
        schedule: ScheduleEntry,
        content: ContentItem,
        publication: Publication | None,
        attempt: PublicationAttempt | None,
    ) -> PlannerEntry:
        return PlannerEntry(
            schedule_id=int(schedule.id),
            channel_id=int(schedule.channel_id),
            content_item_id=int(schedule.content_item_id),
            content_revision=int(schedule.content_revision),
            content_title=content.title,
            content_kind=str(content.kind),
            scheduled_at=as_utc(schedule.scheduled_at),
            timezone=schedule.timezone,
            schedule_status=str(schedule.status),
            repeat_rule=dict(schedule.repeat_rule or {}),
            publication_id=(int(publication.id) if publication is not None else None),
            publication_status=(
                str(publication.status) if publication is not None else None
            ),
            telegram_message_ids=(
                [int(value) for value in publication.telegram_message_ids]
                if publication is not None and publication.telegram_message_ids
                else None
            ),
            result_link=(publication.result_link if publication is not None else None),
            last_error=(publication.last_error if publication is not None else None),
            attempt_number=(int(attempt.attempt) if attempt is not None else None),
            attempt_status=(str(attempt.status) if attempt is not None else None),
            attempt_started_at=(attempt.started_at if attempt is not None else None),
            attempt_finished_at=(attempt.finished_at if attempt is not None else None),
            legacy_post_task_id=(
                int(publication.legacy_post_task_id)
                if publication is not None and publication.legacy_post_task_id is not None
                else None
            ),
        )

    async def list_entries(
        self,
        *,
        channel_id: int,
        start: datetime,
        end: datetime,
        statuses: Iterable[str] | None = None,
        limit: int = 500,
    ) -> list[PlannerEntry]:
        start_utc = as_utc(start)
        end_utc = as_utc(end)
        if end_utc <= start_utc:
            raise PlannerError("planner range end must be after start")

        stmt = (
            select(ScheduleEntry, ContentItem, Publication)
            .join(ContentItem, ContentItem.id == ScheduleEntry.content_item_id)
            .outerjoin(Publication, Publication.schedule_entry_id == ScheduleEntry.id)
            .where(
                ScheduleEntry.channel_id == int(channel_id),
                ScheduleEntry.scheduled_at >= start_utc,
                ScheduleEntry.scheduled_at < end_utc,
            )
            .order_by(ScheduleEntry.scheduled_at.asc(), ScheduleEntry.id.asc())
            .limit(max(1, min(int(limit), 1000)))
        )
        normalized_statuses = {str(value) for value in (statuses or []) if str(value)}
        if normalized_statuses:
            stmt = stmt.where(ScheduleEntry.status.in_(normalized_statuses))

        rows = list((await self.session.execute(stmt)).all())
        attempts = await self._current_attempts(row[2] for row in rows)
        return [
            self._entry(
                schedule,
                content,
                publication,
                attempts.get(int(publication.id)) if publication is not None else None,
            )
            for schedule, content, publication in rows
        ]

    async def _locked_schedule(
        self, *, channel_id: int, schedule_id: int
    ) -> tuple[ScheduleEntry, Publication | None, PostTask | None]:
        result = await self.session.execute(
            select(ScheduleEntry)
            .where(
                ScheduleEntry.id == int(schedule_id),
                ScheduleEntry.channel_id == int(channel_id),
            )
            .with_for_update()
        )
        schedule = result.scalar_one_or_none()
        if schedule is None:
            raise PlannerNotFoundError("schedule entry not found")

        publication = (
            await self.session.execute(
                select(Publication)
                .where(Publication.schedule_entry_id == int(schedule.id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        task = None
        if publication is not None and publication.legacy_post_task_id is not None:
            task = await self.session.get(PostTask, int(publication.legacy_post_task_id))
        return schedule, publication, task

    @staticmethod
    def _ensure_mutable(
        schedule: ScheduleEntry,
        publication: Publication | None,
        task: PostTask | None,
    ) -> None:
        if str(schedule.status) != "pending":
            raise PlannerConflictError(
                f"schedule is no longer mutable (status={schedule.status})"
            )
        if publication is not None and str(publication.status) not in {"queued"}:
            raise PlannerConflictError(
                f"publication is no longer queued (status={publication.status})"
            )
        if task is not None and str(task.status) != "pending":
            raise PlannerConflictError(
                f"scheduler task is no longer pending (status={task.status})"
            )

    async def reschedule(
        self,
        *,
        channel_id: int,
        schedule_id: int,
        scheduled_at: datetime,
        timezone_name: str | None = None,
    ) -> PlannerEntry:
        schedule, publication, task = await self._locked_schedule(
            channel_id=channel_id, schedule_id=schedule_id
        )
        self._ensure_mutable(schedule, publication, task)
        when = as_utc(scheduled_at)
        schedule.scheduled_at = when
        if timezone_name is not None:
            schedule.timezone = timezone_name
        if task is not None:
            task.scheduled_at = when
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return await self.get_entry(channel_id=channel_id, schedule_id=schedule_id)

    async def cancel(self, *, channel_id: int, schedule_id: int) -> PlannerEntry:
        schedule, publication, task = await self._locked_schedule(
            channel_id=channel_id, schedule_id=schedule_id
        )
        self._ensure_mutable(schedule, publication, task)
        schedule.status = "cancelled"
        if publication is not None:
            publication.status = "cancelled"
            publication.last_error = None
        if task is not None:
            task.status = "cancelled"
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return await self.get_entry(channel_id=channel_id, schedule_id=schedule_id)

    async def get_entry(self, *, channel_id: int, schedule_id: int) -> PlannerEntry:
        result = await self.session.execute(
            select(ScheduleEntry, ContentItem, Publication)
            .join(ContentItem, ContentItem.id == ScheduleEntry.content_item_id)
            .outerjoin(Publication, Publication.schedule_entry_id == ScheduleEntry.id)
            .where(
                ScheduleEntry.id == int(schedule_id),
                ScheduleEntry.channel_id == int(channel_id),
            )
        )
        row = result.one_or_none()
        if row is None:
            raise PlannerNotFoundError("schedule entry not found")
        schedule, content, publication = row
        attempts = await self._current_attempts([publication])
        return self._entry(
            schedule,
            content,
            publication,
            attempts.get(int(publication.id)) if publication is not None else None,
        )
