from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CanonicalPublicationAtomicHandoffClaimResult,
)
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlanner
from app.services.canonical_publication_legacy_transport_handoff import (
    CUTOVER_META_KEY,
    _mapping,
)
from app.services.canonical_publication_linked_repeat_parity import (
    CanonicalPublicationLinkedRepeatParityService,
)
from app.services.scheduling import as_utc


_CUTOVER_STATUS = "canonical_cutover"


class CanonicalPublicationLinkedRepeatAtomicHandoffService:
    """Atomically transfer one pristine linked fixed-delay repeat occurrence.

    This first repeat handoff profile is intentionally limited to the read-only parity
    proven by `CanonicalPublicationLinkedRepeatParityService`: empty/explicit-silent
    runtime plus exact fixed-delay repeat cadence/lineage. Effect compositions are widened
    separately.

    The legacy pending-task CAS, parity proof, transport retirement and canonical repeat
    claim share one AsyncSession. The first successful authority commit therefore cannot
    expose a transport-free queued occurrence: durable state is already canonical
    `sending + Attempt #1 + lease`. Repeat claim is allowed only when the caller proves a
    continuation worker is available to recover successor creation after terminal publish.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rollback_result(
        self,
        publication_id: int,
        outcome: str,
        task_id: int | None = None,
    ) -> CanonicalPublicationAtomicHandoffClaimResult:
        await self.session.rollback()
        return CanonicalPublicationAtomicHandoffClaimResult(
            publication_id=int(publication_id),
            outcome=outcome,  # type: ignore[arg-type]
            legacy_post_task_id=(int(task_id) if task_id is not None else None),
        )

    async def claim_linked_repeat(
        self,
        publication_id: int,
        *,
        holder: str,
        ttl_seconds: int,
        at: datetime | None = None,
        allow_repeat: bool = False,
    ) -> CanonicalPublicationAtomicHandoffClaimResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_publication_id = 0
        if safe_publication_id <= 0 or not allow_repeat:
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
                return await self._rollback_result(safe_publication_id, "ineligible")
            task_id = int(publication.legacy_post_task_id)

            claimed_cutover = await self.session.execute(
                update(PostTask)
                .where(PostTask.id == task_id, PostTask.status == "pending")
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
                    select(PostTask).where(PostTask.id == task_id).with_for_update()
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
            ):
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )

            parity = CanonicalPublicationLinkedRepeatParityService().prove(
                task=task,
                publication=publication,
                schedule=schedule,
                plan=plan,
            )
            if parity is None:
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
                "repeat": True,
                "repeat_group_id": int(parity.repeat_group_id),
                "repeat_seconds": int(parity.repeat_seconds),
                "repeat_root": bool(parity.root_occurrence),
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

            claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                self.session
            ).claim_supported(
                publication_id=safe_publication_id,
                holder=holder,
                ttl_seconds=ttl_seconds,
                now=current,
                allow_repeat=True,
            )
            if claim is None:
                # Pre-commit claim rejection rolls back every cutover mutation above.
                # A post-commit snapshot/renew failure may already be recovery-owned;
                # caller must classify durable state before retrying provider work.
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
