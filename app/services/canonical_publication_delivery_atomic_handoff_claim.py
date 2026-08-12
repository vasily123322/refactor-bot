from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CUTOVER_META_KEY,
    _legacy_intent_matches,
    _mapping,
    _nonrepeat,
    _supported_runtime_options,
)
from app.services.scheduling import as_utc


_CUTOVER_STATUS = "canonical_cutover"


@dataclass(frozen=True, slots=True)
class CanonicalPublicationAtomicHandoffClaimResult:
    publication_id: int
    outcome: Literal[
        "claimed",
        "ineligible",
        "contention",
        "conflict",
        "claim_unavailable",
    ]
    legacy_post_task_id: int | None = None
    claim: CanonicalPublicationDeliveryClaim | None = None


class CanonicalPublicationAtomicHandoffClaimService:
    """Transfer linked execution authority and claim canonical delivery in one commit.

    The legacy `pending -> canonical_cutover` CAS remains the serialization point against
    SchedulerTaskLease acquisition. All established transport/intention parity checks are
    reused from the handoff service, but successful retirement is deliberately left
    uncommitted until `CanonicalPublicationDeliveryCapabilityClaimService` transitions
    the same Publication to `sending`, inserts Attempt #1 and creates the exact delivery
    lease in this same AsyncSession.

    Any pre-claim rejection rolls the transaction back, restoring the linked pending
    PostTask. A process crash before the canonical claim commit also rolls back. A crash
    after that commit cannot leave a transport-free queued orphan: durable state is
    already `sending + delivery lease`, which is owned by fail-closed recovery.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rollback_result(
        self,
        publication_id: int,
        outcome: Literal["ineligible", "contention", "conflict", "claim_unavailable"],
        task_id: int | None = None,
    ) -> CanonicalPublicationAtomicHandoffClaimResult:
        await self.session.rollback()
        return CanonicalPublicationAtomicHandoffClaimResult(
            publication_id=int(publication_id),
            outcome=outcome,
            legacy_post_task_id=(int(task_id) if task_id is not None else None),
        )

    async def claim_linked(
        self,
        publication_id: int,
        *,
        holder: str,
        ttl_seconds: int,
        at: datetime | None = None,
        allow_time_autodelete: bool = False,
    ) -> CanonicalPublicationAtomicHandoffClaimResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_publication_id = 0
        if safe_publication_id <= 0:
            return CanonicalPublicationAtomicHandoffClaimResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )
        current = as_utc(at or datetime.now(timezone.utc))

        try:
            publication = (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.id == safe_publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                publication is None
                or publication.status != "queued"
                or int(publication.attempt_count or 0) != 0
                or publication.legacy_post_task_id is None
                or publication.telegram_message_ids not in (None, [])
                or publication.result_link is not None
                or publication.last_error is not None
            ):
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                )
            task_id = int(publication.legacy_post_task_id)

            claimed_cutover = await self.session.execute(
                update(PostTask)
                .where(
                    PostTask.id == task_id,
                    PostTask.status == "pending",
                )
                .values(status=_CUTOVER_STATUS)
                .execution_options(synchronize_session=False)
            )
            if int(claimed_cutover.rowcount or 0) != 1:
                return await self._rollback_result(
                    safe_publication_id,
                    "contention",
                    task_id,
                )

            task = (
                await self.session.execute(
                    select(PostTask)
                    .where(PostTask.id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None or str(task.status) != _CUTOVER_STATUS:
                return await self._rollback_result(
                    safe_publication_id,
                    "conflict",
                    task_id,
                )

            scheduler_lease = (
                await self.session.execute(
                    select(SchedulerTaskLease)
                    .where(SchedulerTaskLease.task_id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if scheduler_lease is not None:
                return await self._rollback_result(
                    safe_publication_id,
                    "conflict",
                    task_id,
                )

            canonical_lease = (
                await self.session.execute(
                    select(PublicationDeliveryLease)
                    .where(PublicationDeliveryLease.publication_id == safe_publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if canonical_lease is not None:
                return await self._rollback_result(
                    safe_publication_id,
                    "conflict",
                    task_id,
                )

            if publication.schedule_entry_id is None:
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )
            schedule = (
                await self.session.execute(
                    select(ScheduleEntry)
                    .where(ScheduleEntry.id == int(publication.schedule_entry_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            item = (
                await self.session.execute(
                    select(ContentItem)
                    .where(ContentItem.id == int(publication.content_item_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            revision = (
                await self.session.execute(
                    select(ContentRevision)
                    .where(
                        ContentRevision.content_item_id == int(publication.content_item_id),
                        ContentRevision.revision == int(publication.content_revision),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            channel = (
                await self.session.execute(
                    select(Channel)
                    .where(Channel.id == int(publication.channel_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None or item is None or revision is None or channel is None:
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )

            plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
                safe_publication_id,
                at=current,
            )
            if (
                plan is None
                or int(plan.schedule_entry_id) != int(schedule.id)
                or int(plan.content_item_id) != int(item.id)
                or int(plan.content_revision) != int(revision.revision)
                or int(plan.channel_id) != int(channel.id)
                or _supported_runtime_options(plan) is None
                or not _nonrepeat(plan)
                or not _legacy_intent_matches(
                    task=task,
                    publication=publication,
                    plan=plan,
                )
            ):
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )

            schedule_meta = _mapping(schedule.meta)
            publication_meta = _mapping(publication.meta)
            if schedule_meta is None or publication_meta is None:
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )
            legacy_marker = schedule_meta.get("legacy_post_task_id")
            if legacy_marker is not None:
                if isinstance(legacy_marker, bool):
                    return await self._rollback_result(
                        safe_publication_id,
                        "conflict",
                        task_id,
                    )
                try:
                    if int(legacy_marker) != task_id:
                        return await self._rollback_result(
                            safe_publication_id,
                            "conflict",
                            task_id,
                        )
                except (TypeError, ValueError, OverflowError):
                    return await self._rollback_result(
                        safe_publication_id,
                        "conflict",
                        task_id,
                    )
                schedule_meta.pop("legacy_post_task_id", None)

            cutover_meta = {
                "retired": True,
                "retired_at": current.isoformat(),
                "legacy_post_task_id": task_id,
                "source_status": "pending",
                "atomic_claim": True,
            }
            schedule.meta = {
                **schedule_meta,
                CUTOVER_META_KEY: deepcopy(cutover_meta),
            }
            publication.meta = {
                **publication_meta,
                CUTOVER_META_KEY: deepcopy(cutover_meta),
            }
            publication.legacy_post_task_id = None
            await self.session.delete(task)

            # This call owns the first commit. If capability/lifecycle claim is rejected
            # before that commit, its rollback restores every handoff mutation above.
            claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                self.session
            ).claim_supported(
                publication_id=safe_publication_id,
                holder=holder,
                ttl_seconds=ttl_seconds,
                now=current,
                allow_time_autodelete=allow_time_autodelete,
            )
            if claim is None:
                # Normal pre-claim failure has already rolled back. If a later post-claim
                # snapshot/renew step failed, the durable row may already be `sending`;
                # either way provider execution is forbidden and recovery remains safe.
                await self.session.rollback()
                return CanonicalPublicationAtomicHandoffClaimResult(
                    publication_id=safe_publication_id,
                    outcome="claim_unavailable",
                    legacy_post_task_id=task_id,
                )

            return CanonicalPublicationAtomicHandoffClaimResult(
                publication_id=safe_publication_id,
                outcome="claimed",
                legacy_post_task_id=task_id,
                claim=claim,
            )
        except Exception:
            await self.session.rollback()
            raise
