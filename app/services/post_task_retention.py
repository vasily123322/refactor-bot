from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


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
    # automatically by the unsuccessful cleanup slice.
    return bool(payload.get("result_ids")) or bool(payload.get("result_link"))


def _safe_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _positive_int_state(value: Any) -> tuple[bool, bool]:
    """Return (valid, positive) for legacy/canonical integer option values."""
    if value in (None, False, 0, "0", ""):
        return True, False
    if isinstance(value, bool):
        return False, False
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return False, False
    if parsed < 0:
        return False, False
    return True, parsed > 0


def _autodelete_is_resolved(payload: dict, publication: Publication) -> bool:
    """Allow successful transport retirement only after all delete semantics are done.

    No delete intent is safe without the canonical deleter. If either legacy payload or
    canonical runtime/options contains timer/views/runtime evidence, the durable
    canonical runtime must explicitly say `deleted=True` before PostTask can disappear.
    Ambiguous metadata fails closed.
    """
    meta = _safe_mapping(publication.meta)
    if meta is None:
        return False
    options = _safe_mapping(meta.get("runtime_options"))
    runtime = _safe_mapping(meta.get(AUTODELETE_RUNTIME_META_KEY))
    if options is None or runtime is None:
        return False

    has_delete_intent = False
    for source in (payload, options, runtime):
        for key in (
            "autodelete_seconds",
            "autodelete_effective_seconds",
            "autodelete_views",
            "effective_seconds",
        ):
            if key not in source:
                continue
            valid, positive = _positive_int_state(source.get(key))
            if not valid:
                return False
            has_delete_intent = has_delete_intent or positive

    for source, key in (
        (payload, "autodelete_at"),
        (runtime, "scheduled_at"),
    ):
        raw = source.get(key)
        if raw is not None:
            if not isinstance(raw, str) or not raw.strip() or len(raw) > 128:
                return False
            has_delete_intent = True

    if "autodeleted" in payload:
        legacy_deleted = payload.get("autodeleted")
        if not isinstance(legacy_deleted, bool):
            return False
        has_delete_intent = True

    deleted = runtime.get("deleted")
    if deleted is not None and not isinstance(deleted, bool):
        return False
    if deleted is True:
        return True
    return not has_delete_intent


def _canonical_result_link(value: Any) -> tuple[bool, str | None]:
    if value is None or value == "":
        return True, None
    normalized = normalize_telegram_result_link(value)
    return normalized is not None, normalized


@dataclass(frozen=True, slots=True)
class PostTaskRetentionTick:
    selected: int = 0
    eligible: int = 0
    deleted: int = 0
    skipped_repeat: int = 0
    skipped_delivery_evidence: int = 0
    skipped_canonical_delivery: int = 0
    skipped_pending_autodelete: int = 0
    skipped_content_linkage: int = 0
    skipped_changed: int = 0
    failures: int = 0


class PostTaskRetentionService:
    """Retire proven compatibility tasks while preserving canonical behavior.

    Unsuccessful non-repeat rows keep the original conservative policy. Successful
    rows are a separate opt-in capability and require exact canonical delivery/content
    evidence plus fully-resolved autodelete semantics before transport retirement.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        retention_days: int = 90,
        batch_size: int = 100,
        retire_successful: bool = False,
    ) -> None:
        self.session = session
        self.retention_days = max(7, min(int(retention_days), 3650))
        self.batch_size = max(1, min(int(batch_size), 500))
        self.retire_successful = bool(retire_successful)

    def _lifecycle_predicate(self):
        unsuccessful = and_(
            PostTask.status.in_(tuple(_RETIRABLE_STATUSES)),
            Publication.status.in_(tuple(_RETIRABLE_STATUSES)),
            ScheduleEntry.status.in_(tuple(_RETIRABLE_STATUSES)),
            PublicationAttempt.status.in_(tuple(_RETIRABLE_STATUSES)),
        )
        if not self.retire_successful:
            return unsuccessful
        successful = and_(
            PostTask.status == "done",
            Publication.status == "published",
            ScheduleEntry.status == "completed",
            PublicationAttempt.status == "published",
        )
        return or_(unsuccessful, successful)

    def _candidate_query(self, *, cutoff: datetime, current: datetime, limit: int):
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
            .outerjoin(
                SchedulerTaskLease,
                and_(
                    SchedulerTaskLease.task_id == PostTask.id,
                    SchedulerTaskLease.expires_at > current,
                ),
            )
            .where(
                self._lifecycle_predicate(),
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
        current: datetime,
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
                    and_(
                        SchedulerTaskLease.task_id == PostTask.id,
                        SchedulerTaskLease.expires_at > current,
                    ),
                )
                .where(
                    PostTask.id == int(task_id),
                    self._lifecycle_predicate(),
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

    async def _successful_content_linkage_is_exact(
        self,
        publication: Publication,
        schedule: ScheduleEntry,
    ) -> bool:
        if not (
            int(schedule.channel_id) == int(publication.channel_id)
            and int(schedule.content_item_id) == int(publication.content_item_id)
            and int(schedule.content_revision) == int(publication.content_revision)
        ):
            return False
        row = (
            await self.session.execute(
                select(ContentItem.id)
                .join(
                    ContentRevision,
                    and_(
                        ContentRevision.content_item_id == ContentItem.id,
                        ContentRevision.revision == publication.content_revision,
                    ),
                )
                .where(
                    ContentItem.id == int(publication.content_item_id),
                    ContentItem.channel_id == int(publication.channel_id),
                    ContentItem.kind == "post",
                    ContentItem.current_revision == int(publication.content_revision),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return row is not None

    @staticmethod
    def _successful_delivery_is_exact(
        task: PostTask,
        publication: Publication,
        attempt: PublicationAttempt,
    ) -> bool:
        payload = dict(task.payload or {})
        task_ids = normalize_telegram_message_ids(payload.get("result_ids"))
        publication_ids = normalize_telegram_message_ids(
            publication.telegram_message_ids
        )
        attempt_ids = normalize_telegram_message_ids(attempt.telegram_message_ids)
        if not task_ids or not (
            task_ids == publication_ids == attempt_ids
        ):
            return False

        task_link_valid, task_link = _canonical_result_link(payload.get("result_link"))
        publication_link_valid, publication_link = _canonical_result_link(
            publication.result_link
        )
        return (
            task_link_valid
            and publication_link_valid
            and task_link == publication_link
        )

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
                    self._candidate_query(
                        cutoff=cutoff,
                        current=current,
                        limit=self.batch_size * 5,
                    )
                )
            ).scalars().all()
        ]

        eligible = 0
        deleted = 0
        skipped_repeat = 0
        skipped_delivery_evidence = 0
        skipped_canonical_delivery = 0
        skipped_pending_autodelete = 0
        skipped_content_linkage = 0
        skipped_changed = 0
        failures = 0

        for task_id in candidate_ids:
            if deleted >= self.batch_size:
                break
            try:
                locked = await self._locked_candidate(
                    task_id,
                    cutoff=cutoff,
                    current=current,
                )
                if locked is None:
                    await self.session.rollback()
                    skipped_changed += 1
                    continue
                task, publication, attempt, schedule = locked

                task_status = str(task.status)
                unsuccessful = (
                    task_status in _RETIRABLE_STATUSES
                    and str(publication.status) == task_status
                    and str(attempt.status) == task_status
                    and str(schedule.status) == task_status
                )
                successful = (
                    self.retire_successful
                    and task_status == "done"
                    and str(publication.status) == "published"
                    and str(attempt.status) == "published"
                    and str(schedule.status) == "completed"
                )
                if not (unsuccessful or successful):
                    await self.session.rollback()
                    skipped_changed += 1
                    continue

                payload = dict(task.payload or {})
                if _has_repeat_lineage(payload) or bool(schedule.repeat_rule):
                    await self.session.rollback()
                    skipped_repeat += 1
                    continue

                if successful:
                    if not await self._successful_content_linkage_is_exact(
                        publication, schedule
                    ):
                        await self.session.rollback()
                        skipped_content_linkage += 1
                        continue
                    if not self._successful_delivery_is_exact(
                        task, publication, attempt
                    ):
                        await self.session.rollback()
                        skipped_canonical_delivery += 1
                        continue
                    if not _autodelete_is_resolved(payload, publication):
                        await self.session.rollback()
                        skipped_pending_autodelete += 1
                        continue
                elif _has_delivery_evidence(payload):
                    await self.session.rollback()
                    skipped_delivery_evidence += 1
                    continue

                # Re-check the lease row while the candidate is locked. Terminal tasks
                # should not normally acquire a fresh lease, but a late active lease
                # must still block deletion. Expired leases are compatibility debris and
                # are deleted explicitly so unmanaged SQLite (FK enforcement may be off)
                # cannot retain an orphan after the PostTask row is retired.
                lease = (
                    await self.session.execute(
                        select(SchedulerTaskLease)
                        .where(SchedulerTaskLease.task_id == int(task_id))
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if lease is not None:
                    if _utc(lease.expires_at) > current:
                        await self.session.rollback()
                        skipped_changed += 1
                        continue
                    await self.session.delete(lease)

                eligible += 1
                publication.legacy_post_task_id = None
                publication.meta = {
                    **dict(publication.meta or {}),
                    _RETENTION_META_KEY: {
                        "retired": True,
                        "retired_at": current.isoformat(),
                        "terminal_status": task_status,
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
            skipped_canonical_delivery=skipped_canonical_delivery,
            skipped_pending_autodelete=skipped_pending_autodelete,
            skipped_content_linkage=skipped_content_linkage,
            skipped_changed=skipped_changed,
            failures=failures,
        )