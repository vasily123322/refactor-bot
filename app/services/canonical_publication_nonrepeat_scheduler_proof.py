from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    _legacy_intent_matches,
    _mapping,
    _nonrepeat,
    _supported_runtime_options,
)
from app.services.canonical_publication_nonrepeat_authority import (
    canonical_publication_delivery_nonrepeat_plain_started,
)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationNonrepeatSchedulerProof:
    publication_id: int
    legacy_post_task_id: int
    profile: str


class CanonicalPublicationNonrepeatSchedulerProofService:
    """Read-only proof that one pending legacy task may yield to canonical primary.

    This service deliberately owns no cutover mutation. It never takes a scheduler or
    canonical lease, never changes PostTask/Publication/ScheduleEntry, never commits,
    and never calls a provider. The canonical worker must independently win the atomic
    handoff/claim after the legacy scheduler has yielded.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def prove_plain(
        self,
        *,
        task_id: int,
        at: datetime | None = None,
    ) -> CanonicalPublicationNonrepeatSchedulerProof | None:
        if not canonical_publication_delivery_nonrepeat_plain_started():
            return None
        try:
            safe_task_id = int(task_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_task_id <= 0:
            return None

        publications = list(
            (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.legacy_post_task_id == safe_task_id)
                    .limit(2)
                )
            ).scalars().all()
        )
        if len(publications) != 1:
            return None
        publication = publications[0]
        if (
            publication.status != "queued"
            or int(publication.attempt_count or 0) != 0
            or publication.legacy_post_task_id is None
            or int(publication.legacy_post_task_id) != safe_task_id
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
            or publication.schedule_entry_id is None
        ):
            return None

        task = await self.session.get(PostTask, safe_task_id, populate_existing=True)
        if task is None or str(task.status) != "pending":
            return None

        # Any historical scheduler lease, including an expired one, is a recovery
        # authority barrier. Read-only admission must not race or reinterpret it.
        scheduler_lease = (
            await self.session.execute(
                select(SchedulerTaskLease).where(
                    SchedulerTaskLease.task_id == safe_task_id
                )
            )
        ).scalar_one_or_none()
        if scheduler_lease is not None:
            return None
        canonical_lease = (
            await self.session.execute(
                select(PublicationDeliveryLease).where(
                    PublicationDeliveryLease.publication_id == int(publication.id)
                )
            )
        ).scalar_one_or_none()
        if canonical_lease is not None:
            return None

        schedule = await self.session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id),
            populate_existing=True,
        )
        if schedule is None:
            return None
        schedule_meta = _mapping(schedule.meta)
        publication_meta = _mapping(publication.meta)
        if schedule_meta is None or publication_meta is None:
            return None
        legacy_marker = schedule_meta.get("legacy_post_task_id")
        if legacy_marker is not None:
            if isinstance(legacy_marker, bool):
                return None
            try:
                if int(legacy_marker) != safe_task_id:
                    return None
            except (TypeError, ValueError, OverflowError):
                return None

        plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
            int(publication.id),
            at=at,
        )
        if (
            plan is None
            or int(plan.schedule_entry_id) != int(schedule.id)
            or int(plan.content_item_id) != int(publication.content_item_id)
            or int(plan.content_revision) != int(publication.content_revision)
            or int(plan.channel_id) != int(publication.channel_id)
            or not _nonrepeat(plan)
        ):
            return None

        runtime_options = _supported_runtime_options(plan)
        if runtime_options is None or set(runtime_options) - {"silent"}:
            return None
        if not _legacy_intent_matches(
            task=task,
            publication=publication,
            plan=plan,
        ):
            return None

        return CanonicalPublicationNonrepeatSchedulerProof(
            publication_id=int(publication.id),
            legacy_post_task_id=safe_task_id,
            profile="plain",
        )
