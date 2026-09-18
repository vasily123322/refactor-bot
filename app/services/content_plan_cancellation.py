from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.content_plan_publication_controls import (
    content_plan_schedule_state_token,
)
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    has_canonical_execution_authority,
)


ContentPlanDeleteOutcome = Literal[
    "cancelled",
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



class ContentPlanCancellationService:
    """Cancel canonical content-plan work using Publication/ScheduleEntry authority."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def delete_canonical_publication(
        self,
        publication_id: int,
        *,
        expected_schedule_token: str | None = None,
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

            expected_scheduled_at = schedule.scheduled_at
            expected_repeat_rule = dict(schedule.repeat_rule or {})
            if expected_schedule_token is not None:
                actual_schedule_token = content_plan_schedule_state_token(
                    schedule_entry_id=int(schedule.id),
                    scheduled_at=schedule.scheduled_at,
                    repeat_rule=expected_repeat_rule,
                )
                if (
                    not str(expected_schedule_token).strip()
                    or str(expected_schedule_token).strip().lower()
                    != actual_schedule_token
                ):
                    await session.rollback()
                    return ContentPlanDeleteResult(
                        outcome="cannot_cancel",
                        reason="stale_schedule_state",
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
                    ScheduleEntry.scheduled_at == expected_scheduled_at,
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
