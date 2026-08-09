from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease


_RETIRABLE_STATUSES = frozenset({"failed", "skipped", "cancelled"})
_RETENTION_META_KEY = "legacy_transport_retention"


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _has_repeat_lineage(payload: dict) -> bool:
    return bool(payload.get("repeat_on", False)) or payload.get("repeat_group_id") is not None


def _has_delivery_evidence(payload: dict) -> bool:
    # Be deliberately conservative. Even malformed/non-canonical legacy result data
    # means the row may refer to a real Telegram side effect and should not be retired
    # automatically by this first cleanup slice.
    return bool(payload.get("result_ids")) or bool(payload.get("result_link"))


@dataclass(frozen=True, slots=True)
class PostTaskRetentionTick:
    selected: int = 0
    eligible: int = 0
    deleted: int = 0
    skipped_repeat: int = 0
    skipped_delivery_evidence: int = 0
    skipped_changed: int = 0
    failures: int = 0


class PostTaskRetentionService:
    """Retire only canonicalized, unsuccessful non-repeat compatibility tasks.

    The first retention slice intentionally excludes successful publications because
    legacy edit callbacks and autodelete workflows may still refer to their PostTask.
    Repeat lineage is also excluded because repeat_group_id still names the root task.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        retention_days: int = 90,
        batch_size: int = 100,
    ) -> None:
        self.session = session
        self.retention_days = max(7, min(int(retention_days), 3650))
        self.batch_size = max(1, min(int(batch_size), 500))

    def _candidate_query(self, *, cutoff: datetime, limit: int):
        return (
            select(PostTask.id)
            .join(Publication, Publication.legacy_post_task_id == PostTask.id)
            .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
            .join(
                PublicationAttempt,
                and_(
                    PublicationAttempt.publication_id == Publication.id,
                    PublicationAttempt.attempt == Publication.attempt_count,
                ),
            )
            .outerjoin(SchedulerTaskLease, SchedulerTaskLease.task_id == PostTask.id)
            .where(
                PostTask.status.in_(tuple(_RETIRABLE_STATUSES)),
                Publication.status.in_(tuple(_RETIRABLE_STATUSES)),
                ScheduleEntry.status.in_(tuple(_RETIRABLE_STATUSES)),
                PublicationAttempt.status.in_(tuple(_RETIRABLE_STATUSES)),
                PublicationAttempt.finished_at.is_not(None),
                PublicationAttempt.finished_at <= cutoff,
                SchedulerTaskLease.task_id.is_(None),
            )
            .order_by(PublicationAttempt.finished_at.asc(), PostTask.id.asc())
            .limit(max(1, int(limit)))
        )

    async def _locked_candidate(
        self,
        task_id: int,
        *,
        cutoff: datetime,
    ) -> tuple[PostTask, Publication, PublicationAttempt, ScheduleEntry] | None:
        row = (
            await self.session.execute(
                select(PostTask, Publication, PublicationAttempt, ScheduleEntry)
                .join(Publication, Publication.legacy_post_task_id == PostTask.id)
                .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
                .join(
                    PublicationAttempt,
                    and_(
                        PublicationAttempt.publication_id == Publication.id,
                        PublicationAttempt.attempt == Publication.attempt_count,
                    ),
                )
                .outerjoin(
                    SchedulerTaskLease,
                    SchedulerTaskLease.task_id == PostTask.id,
                )
                .where(
                    PostTask.id == int(task_id),
                    PostTask.status.in_(tuple(_RETIRABLE_STATUSES)),
                    Publication.status.in_(tuple(_RETIRABLE_STATUSES)),
                    ScheduleEntry.status.in_(tuple(_RETIRABLE_STATUSES)),
                    PublicationAttempt.status.in_(tuple(_RETIRABLE_STATUSES)),
                    PublicationAttempt.finished_at.is_not(None),
                    PublicationAttempt.finished_at <= cutoff,
                    SchedulerTaskLease.task_id.is_(None),
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            return None
        task, publication, attempt, schedule = row
        return task, publication, attempt, schedule

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> PostTaskRetentionTick:
        current = _utc(now)
        cutoff = current - timedelta(days=self.retention_days)

        # Overscan is bounded so Python-side conservative filters do not let one
        # repeat series permanently starve unrelated safe candidates.
        candidate_ids = [
            int(value)
            for value in (
                await self.session.execute(
                    self._candidate_query(cutoff=cutoff, limit=self.batch_size * 5)
                )
            ).scalars().all()
        ]

        eligible = 0
        deleted = 0
        skipped_repeat = 0
        skipped_delivery_evidence = 0
        skipped_changed = 0
        failures = 0

        for task_id in candidate_ids:
            if deleted >= self.batch_size:
                break
            try:
                locked = await self._locked_candidate(task_id, cutoff=cutoff)
                if locked is None:
                    await self.session.rollback()
                    skipped_changed += 1
                    continue
                task, publication, attempt, schedule = locked

                # Require exact state agreement across compatibility/canonical rows.
                status = str(task.status)
                if not (
                    status in _RETIRABLE_STATUSES
                    and str(publication.status) == status
                    and str(attempt.status) == status
                    and str(schedule.status) == status
                ):
                    await self.session.rollback()
                    skipped_changed += 1
                    continue

                payload = dict(task.payload or {})
                if _has_repeat_lineage(payload):
                    await self.session.rollback()
                    skipped_repeat += 1
                    continue
                if _has_delivery_evidence(payload):
                    await self.session.rollback()
                    skipped_delivery_evidence += 1
                    continue

                eligible += 1
                publication.legacy_post_task_id = None
                publication.meta = {
                    **dict(publication.meta or {}),
                    _RETENTION_META_KEY: {
                        "retired": True,
                        "retired_at": current.isoformat(),
                        "terminal_status": status,
                    },
                }
                await self.session.delete(task)
                await self.session.commit()
                deleted += 1
            except Exception:
                await self.session.rollback()
                failures += 1

        return PostTaskRetentionTick(
            selected=len(candidate_ids),
            eligible=eligible,
            deleted=deleted,
            skipped_repeat=skipped_repeat,
            skipped_delivery_evidence=skipped_delivery_evidence,
            skipped_changed=skipped_changed,
            failures=failures,
        )
