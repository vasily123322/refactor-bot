from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease


_CANCEL_STATUS = "canonical_cancel"
_CANCEL_META_KEY = "canonical_pending_cancel"


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CanonicalPendingPublicationCancelResult:
    post_task_id: int
    outcome: Literal[
        "cancelled",
        "legacy_deleted",
        "not_found",
        "contention",
        "conflict",
        "ineligible",
    ]
    publication_id: int | None = None
    schedule_entry_id: int | None = None


class CanonicalPendingPublicationCancelService:
    """Cancel one pending compatibility transport without racing delivery authority.

    The PostTask status CAS is the serialization point against both the legacy scheduler
    lease claim and canonical atomic handoff, which also require ``status == pending``.
    The control never calls Telegram. After winning the CAS it rejects any historical
    scheduler lease (including expired recovery barriers) and any canonical delivery
    lease before changing durable state.

    A linked pristine queued Publication is cancelled together with its ScheduleEntry and
    the compatibility row is removed in one commit. An unlinked legacy pending task keeps
    historical queue-delete behavior, but now through the same CAS/lease safety boundary.
    Non-pending/historical rows are deliberately not deleted by this migration control.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rollback(
        self,
        task_id: int,
        outcome: Literal["not_found", "contention", "conflict", "ineligible"],
        *,
        publication_id: int | None = None,
        schedule_entry_id: int | None = None,
    ) -> CanonicalPendingPublicationCancelResult:
        await self.session.rollback()
        return CanonicalPendingPublicationCancelResult(
            post_task_id=int(task_id),
            outcome=outcome,
            publication_id=publication_id,
            schedule_entry_id=schedule_entry_id,
        )

    async def cancel_owned_pending(
        self,
        *,
        post_task_id: int,
        tg_user_id: int,
        at: datetime | None = None,
    ) -> CanonicalPendingPublicationCancelResult:
        try:
            task_id = int(post_task_id)
            user_id = int(tg_user_id)
        except (TypeError, ValueError, OverflowError):
            return CanonicalPendingPublicationCancelResult(0, "ineligible")
        if task_id <= 0 or user_id <= 0:
            return CanonicalPendingPublicationCancelResult(task_id, "ineligible")
        current = _utc(at)

        try:
            owned = (
                await self.session.execute(
                    select(PostTask.id)
                    .join(Channel, Channel.id == PostTask.channel_id)
                    .join(Client, Client.id == Channel.owner_id)
                    .where(
                        PostTask.id == task_id,
                        Client.tg_user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if owned is None:
                return await self._rollback(task_id, "not_found")

            claimed = await self.session.execute(
                update(PostTask)
                .where(PostTask.id == task_id, PostTask.status == "pending")
                .values(status=_CANCEL_STATUS)
                .execution_options(synchronize_session=False)
            )
            if int(claimed.rowcount or 0) != 1:
                return await self._rollback(task_id, "contention")

            task = (
                await self.session.execute(
                    select(PostTask)
                    .where(PostTask.id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None or str(task.status) != _CANCEL_STATUS:
                return await self._rollback(task_id, "conflict")

            scheduler_lease = (
                await self.session.execute(
                    select(SchedulerTaskLease)
                    .where(SchedulerTaskLease.task_id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if scheduler_lease is not None:
                return await self._rollback(task_id, "conflict")

            publications = list(
                (
                    await self.session.execute(
                        select(Publication)
                        .where(Publication.legacy_post_task_id == task_id)
                        .with_for_update()
                        .limit(2)
                    )
                ).scalars().all()
            )
            if len(publications) > 1:
                return await self._rollback(task_id, "conflict")

            if not publications:
                await self.session.delete(task)
                await self.session.commit()
                return CanonicalPendingPublicationCancelResult(
                    post_task_id=task_id,
                    outcome="legacy_deleted",
                )

            publication = publications[0]
            publication_id = int(publication.id)
            if (
                publication.status != "queued"
                or int(publication.attempt_count or 0) != 0
                or publication.legacy_post_task_id is None
                or int(publication.legacy_post_task_id) != task_id
                or publication.telegram_message_ids not in (None, [])
                or publication.result_link is not None
                or publication.last_error is not None
                or publication.schedule_entry_id is None
                or int(publication.channel_id) != int(task.channel_id)
            ):
                return await self._rollback(
                    task_id,
                    "ineligible",
                    publication_id=publication_id,
                )

            canonical_lease = (
                await self.session.execute(
                    select(PublicationDeliveryLease)
                    .where(PublicationDeliveryLease.publication_id == publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if canonical_lease is not None:
                return await self._rollback(
                    task_id,
                    "conflict",
                    publication_id=publication_id,
                )

            schedule = (
                await self.session.execute(
                    select(ScheduleEntry)
                    .where(
                        ScheduleEntry.id == int(publication.schedule_entry_id)
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                schedule is None
                or schedule.status != "pending"
                or int(schedule.channel_id) != int(publication.channel_id)
                or int(schedule.content_item_id) != int(publication.content_item_id)
                or int(schedule.content_revision) != int(publication.content_revision)
            ):
                return await self._rollback(
                    task_id,
                    "conflict",
                    publication_id=publication_id,
                    schedule_entry_id=(int(schedule.id) if schedule is not None else None),
                )

            schedule_meta = schedule.meta
            publication_meta = publication.meta
            if not isinstance(schedule_meta, Mapping) or not isinstance(
                publication_meta, Mapping
            ):
                return await self._rollback(
                    task_id,
                    "conflict",
                    publication_id=publication_id,
                    schedule_entry_id=int(schedule.id),
                )
            legacy_marker = dict(schedule_meta).get("legacy_post_task_id")
            if legacy_marker is not None:
                if isinstance(legacy_marker, bool):
                    return await self._rollback(
                        task_id,
                        "conflict",
                        publication_id=publication_id,
                        schedule_entry_id=int(schedule.id),
                    )
                try:
                    if int(legacy_marker) != task_id:
                        return await self._rollback(
                            task_id,
                            "conflict",
                            publication_id=publication_id,
                            schedule_entry_id=int(schedule.id),
                        )
                except (TypeError, ValueError, OverflowError):
                    return await self._rollback(
                        task_id,
                        "conflict",
                        publication_id=publication_id,
                        schedule_entry_id=int(schedule.id),
                    )

            marker = {
                "version": 1,
                "cancelled": True,
                "cancelled_at": current.isoformat(),
                "cancelled_by_tg_user_id": user_id,
                "legacy_post_task_id": task_id,
                "source_status": "pending",
            }
            new_schedule_meta = deepcopy(dict(schedule_meta))
            new_schedule_meta.pop("legacy_post_task_id", None)
            new_schedule_meta[_CANCEL_META_KEY] = deepcopy(marker)
            schedule.meta = new_schedule_meta
            schedule.status = "cancelled"

            new_publication_meta = deepcopy(dict(publication_meta))
            new_publication_meta[_CANCEL_META_KEY] = deepcopy(marker)
            publication.meta = new_publication_meta
            publication.status = "cancelled"
            publication.legacy_post_task_id = None

            await self.session.delete(task)
            await self.session.commit()
            return CanonicalPendingPublicationCancelResult(
                post_task_id=task_id,
                outcome="cancelled",
                publication_id=publication_id,
                schedule_entry_id=int(schedule.id),
            )
        except Exception:
            await self.session.rollback()
            raise
