from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    has_canonical_execution_authority,
)


ContentPlanDeleteOutcome = Literal[
    "cancelled",
    "legacy_deleted",
    "already_absent",
    "cannot_cancel",
]


@dataclass(frozen=True, slots=True)
class ContentPlanDeleteResult:
    outcome: ContentPlanDeleteOutcome
    reason: str | None = None
    compatibility_retired: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome != "cannot_cancel"


@dataclass(frozen=True, slots=True)
class _CanonicalCancellation:
    publication_id: int
    schedule_entry_id: int
    post_task_id: int


def _is_legacy_mixed_time_views(payload: dict | None) -> bool:
    """Recognise the legacy mixed time+views owner without claiming it canonically."""

    data = payload if isinstance(payload, dict) else {}
    raw = data.get("autodelete")
    cfg = raw if isinstance(raw, dict) else {}
    try:
        seconds = int(cfg.get("seconds") or data.get("autodelete_seconds") or 0)
    except (TypeError, ValueError, OverflowError):
        seconds = 0
    try:
        views = int(
            cfg.get("views")
            or data.get("delete_after_views")
            or data.get("autodelete_views")
            or 0
        )
    except (TypeError, ValueError, OverflowError):
        views = 0
    return seconds > 0 and views > 0


class ContentPlanCancellationService:
    """Cancel canonical content-plan work before retiring its compatibility task.

    Canonical delivery and legacy scheduler claims both use compare-and-set state
    transitions. Cancellation competes with those claims instead of treating
    ``PostTask`` deletion as cancellation authority.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def delete_canonical_publication(
        self, publication_id: int
    ) -> ContentPlanDeleteResult:
        """Cancel one explicitly canonical occurrence without consulting PostTask.

        Persisted execution mode is the sole authority selector. Historical, malformed,
        and intentional-legacy rows fail closed. The cancellation transaction reuses the
        same publication/schedule compare-and-set boundary as compatibility cancellation,
        but deliberately neither reads nor mutates compatibility transport or scheduler
        lease state.
        """

        async with self.session_factory() as session:
            publication = (
                await session.execute(
                    select(Publication)
                    .where(Publication.id == int(publication_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if publication is None:
                await session.rollback()
                return ContentPlanDeleteResult(outcome="already_absent")

            if not has_canonical_execution_authority(publication.execution_mode):
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel",
                    reason="canonical_execution_authority_absent",
                )

            schedule_id = publication.schedule_entry_id
            if schedule_id is None:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="missing_schedule_entry"
                )

            schedule = (
                await session.execute(
                    select(ScheduleEntry)
                    .where(ScheduleEntry.id == int(schedule_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="missing_schedule_entry"
                )

            reason = await self._canonical_execution_barrier_reason(
                session,
                publication=publication,
            )
            if reason is not None:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason=reason
                )

            if publication.status == "cancelled":
                if schedule.status != "cancelled":
                    await session.rollback()
                    return ContentPlanDeleteResult(
                        outcome="cannot_cancel",
                        reason="partial_terminal_cancellation",
                    )
                await session.rollback()
                return ContentPlanDeleteResult(outcome="cancelled")

            if publication.status != "queued":
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel",
                    reason=f"publication_{publication.status or 'unknown'}",
                )
            if schedule.status != "pending":
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel",
                    reason=f"schedule_{schedule.status or 'unknown'}",
                )

            publication_cas = await session.execute(
                update(Publication)
                .where(
                    Publication.id == int(publication.id),
                    Publication.execution_mode == CANONICAL_EXECUTION_MODE,
                    Publication.status == "queued",
                    Publication.attempt_count == 0,
                    Publication.result_link.is_(None),
                    Publication.last_error.is_(None),
                )
                .values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            if int(publication_cas.rowcount or 0) != 1:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="publication_claim_race"
                )

            schedule_cas = await session.execute(
                update(ScheduleEntry)
                .where(
                    ScheduleEntry.id == int(schedule.id),
                    ScheduleEntry.status == "pending",
                )
                .values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            if int(schedule_cas.rowcount or 0) != 1:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="schedule_claim_race"
                )

            await session.commit()
            return ContentPlanDeleteResult(outcome="cancelled")

    async def delete(self, post_task_id: int) -> ContentPlanDeleteResult:
        task_id = int(post_task_id)
        cancellation = await self._cancel_or_delete_legacy(task_id)
        if isinstance(cancellation, ContentPlanDeleteResult):
            return cancellation

        try:
            retired = await self._retire_compatibility(cancellation)
        except Exception:
            # Canonical cancellation is already durable and the PostTask was fenced
            # to ``cancelled``. A failed compatibility cleanup must not resurrect work.
            return ContentPlanDeleteResult(
                outcome="cancelled",
                reason="compatibility_retirement_failed",
                compatibility_retired=False,
            )

        return ContentPlanDeleteResult(
            outcome="cancelled",
            compatibility_retired=retired,
        )

    async def _cancel_or_delete_legacy(
        self, post_task_id: int
    ) -> ContentPlanDeleteResult | _CanonicalCancellation:
        async with self.session_factory() as session:
            publication = (
                await session.execute(
                    select(Publication)
                    .where(Publication.legacy_post_task_id == int(post_task_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if publication is None:
                post = (
                    await session.execute(
                        select(PostTask)
                        .where(PostTask.id == int(post_task_id))
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if post is None:
                    await session.rollback()
                    return ContentPlanDeleteResult(outcome="already_absent")

                # Deliberate legacy fallback: no canonical linkage means this handler
                # keeps the pre-migration delete semantics and does not materialize one.
                await session.delete(post)
                await session.commit()
                return ContentPlanDeleteResult(
                    outcome="legacy_deleted",
                    compatibility_retired=True,
                )

            schedule_id = publication.schedule_entry_id
            if schedule_id is None:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="missing_schedule_entry"
                )

            schedule = (
                await session.execute(
                    select(ScheduleEntry)
                    .where(ScheduleEntry.id == int(schedule_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            post = (
                await session.execute(
                    select(PostTask)
                    .where(PostTask.id == int(post_task_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None or post is None:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="incomplete_compatibility_link"
                )

            if _is_legacy_mixed_time_views(post.payload):
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="legacy_mixed_time_views_owner"
                )

            reason = await self._execution_barrier_reason(
                session,
                publication=publication,
                post_task_id=int(post_task_id),
            )
            if reason is not None:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason=reason
                )

            publication_id = int(publication.id)
            schedule_entry_id = int(schedule.id)
            task_id = int(post.id)

            if publication.status == "cancelled":
                if schedule.status != "cancelled" or post.status != "cancelled":
                    await session.rollback()
                    return ContentPlanDeleteResult(
                        outcome="cannot_cancel",
                        reason="partial_terminal_cancellation",
                    )
                await session.rollback()
                return _CanonicalCancellation(
                    publication_id=publication_id,
                    schedule_entry_id=schedule_entry_id,
                    post_task_id=task_id,
                )

            if publication.status != "queued":
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel",
                    reason=f"publication_{publication.status or 'unknown'}",
                )
            if schedule.status != "pending":
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel",
                    reason=f"schedule_{schedule.status or 'unknown'}",
                )
            if post.status != "pending":
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel",
                    reason=f"post_task_{post.status or 'unknown'}",
                )

            publication_cas = await session.execute(
                update(Publication)
                .where(
                    Publication.id == publication_id,
                    Publication.legacy_post_task_id == int(post_task_id),
                    Publication.status == "queued",
                    Publication.attempt_count == 0,
                    Publication.result_link.is_(None),
                    Publication.last_error.is_(None),
                )
                .values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            if int(publication_cas.rowcount or 0) != 1:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="publication_claim_race"
                )

            schedule_cas = await session.execute(
                update(ScheduleEntry)
                .where(
                    ScheduleEntry.id == schedule_entry_id,
                    ScheduleEntry.status == "pending",
                )
                .values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            if int(schedule_cas.rowcount or 0) != 1:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="schedule_claim_race"
                )

            # Fence the compatibility transport in the same durable transaction.
            # Physical deletion remains a separate second phase after this commit.
            post_cas = await session.execute(
                update(PostTask)
                .where(
                    PostTask.id == task_id,
                    PostTask.status == "pending",
                )
                .values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            if int(post_cas.rowcount or 0) != 1:
                await session.rollback()
                return ContentPlanDeleteResult(
                    outcome="cannot_cancel", reason="legacy_scheduler_claim_race"
                )

            await session.commit()
            return _CanonicalCancellation(
                publication_id=publication_id,
                schedule_entry_id=schedule_entry_id,
                post_task_id=task_id,
            )

    async def _canonical_execution_barrier_reason(
        self,
        session: AsyncSession,
        *,
        publication: Publication,
    ) -> str | None:
        if int(publication.attempt_count or 0) != 0:
            return "publication_attempt_count"
        if publication.result_link is not None:
            return "publication_result"
        if publication.last_error is not None:
            return "publication_error"
        if publication.telegram_message_ids not in (None, []):
            return "publication_provider_result"

        attempt_id = await session.scalar(
            select(PublicationAttempt.id)
            .where(PublicationAttempt.publication_id == int(publication.id))
            .limit(1)
        )
        if attempt_id is not None:
            return "publication_attempt"

        delivery_lease_id = await session.scalar(
            select(PublicationDeliveryLease.publication_id)
            .where(PublicationDeliveryLease.publication_id == int(publication.id))
            .limit(1)
        )
        if delivery_lease_id is not None:
            return "publication_delivery_lease"
        return None

    async def _execution_barrier_reason(
        self,
        session: AsyncSession,
        *,
        publication: Publication,
        post_task_id: int,
    ) -> str | None:
        reason = await self._canonical_execution_barrier_reason(
            session,
            publication=publication,
        )
        if reason is not None:
            return reason

        # Any scheduler lease, including an expired one, is an ambiguous execution
        # barrier by the scheduler's own recovery contract.
        scheduler_lease_id = await session.scalar(
            select(SchedulerTaskLease.task_id)
            .where(SchedulerTaskLease.task_id == int(post_task_id))
            .limit(1)
        )
        if scheduler_lease_id is not None:
            return "legacy_scheduler_lease"
        return None

    async def _retire_compatibility(
        self, cancellation: _CanonicalCancellation
    ) -> bool:
        async with self.session_factory() as session:
            publication = (
                await session.execute(
                    select(Publication)
                    .where(Publication.id == int(cancellation.publication_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if publication is None:
                await session.rollback()
                return False
            if (
                publication.status != "cancelled"
                or publication.legacy_post_task_id != int(cancellation.post_task_id)
                or publication.schedule_entry_id != int(cancellation.schedule_entry_id)
            ):
                await session.rollback()
                return False

            schedule = (
                await session.execute(
                    select(ScheduleEntry)
                    .where(ScheduleEntry.id == int(cancellation.schedule_entry_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            post = (
                await session.execute(
                    select(PostTask)
                    .where(PostTask.id == int(cancellation.post_task_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None or schedule.status != "cancelled":
                await session.rollback()
                return False
            if post is None:
                await session.rollback()
                return True
            if post.status != "cancelled":
                await session.rollback()
                return False

            reason = await self._execution_barrier_reason(
                session,
                publication=publication,
                post_task_id=int(cancellation.post_task_id),
            )
            if reason is not None:
                await session.rollback()
                return False

            unlink = await session.execute(
                update(Publication)
                .where(
                    Publication.id == int(cancellation.publication_id),
                    Publication.status == "cancelled",
                    Publication.legacy_post_task_id == int(cancellation.post_task_id),
                )
                .values(legacy_post_task_id=None)
                .execution_options(synchronize_session=False)
            )
            if int(unlink.rowcount or 0) != 1:
                await session.rollback()
                return False

            await session.delete(post)
            await session.commit()
            return True
