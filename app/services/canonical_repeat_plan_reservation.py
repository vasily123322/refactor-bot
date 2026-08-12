from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_repeat_continuation_authority import (
    CanonicalRepeatContinuationAuthorityService,
)
from app.services.canonical_repeat_planner import (
    CanonicalRepeatPlan,
    CanonicalRepeatPlanner,
)
from app.services.scheduling import as_utc


CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY = "canonical_repeat_plan_reservation"


@dataclass(frozen=True, slots=True)
class CanonicalRepeatPlanReservationResult:
    publication_id: int
    outcome: Literal[
        "reserved",
        "already_reserved",
        "existing_successor",
        "ineligible",
        "conflict",
    ]
    plan: CanonicalRepeatPlan | None = None


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _reservation_snapshot(plan: CanonicalRepeatPlan) -> dict[str, Any]:
    return {
        "version": 1,
        "source_publication_id": int(plan.source_publication_id),
        "source_schedule_entry_id": int(plan.source_schedule_entry_id),
        "repeat_group_id": int(plan.repeat_group_id),
        "channel_id": int(plan.channel_id),
        "content_item_id": int(plan.content_item_id),
        "content_revision": int(plan.content_revision),
        "repeat_seconds": int(plan.repeat_seconds),
        "scheduled_at": as_utc(plan.scheduled_at).isoformat(),
        "runtime_options": deepcopy(plan.runtime_options),
    }


class CanonicalRepeatPlanReservationService:
    """Persist one deterministic repeat plan only under live canonical authority.

    Source Publication/Schedule/exact Attempt are locked and re-proven by the shared
    continuation authority service before planning or metadata mutation. Selection by a
    worker is never authority: a relinked legacy PostTask or canonical-origin drift makes
    reservation immediately ineligible in this transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve_next(
        self,
        publication_id: int,
        *,
        after: datetime | None = None,
    ) -> CanonicalRepeatPlanReservationResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return CanonicalRepeatPlanReservationResult(
                publication_id=0,
                outcome="ineligible",
            )
        if safe_publication_id <= 0:
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )

        authority = await CanonicalRepeatContinuationAuthorityService(
            self.session
        ).lock_and_prove(safe_publication_id)
        if authority is None:
            await self.session.rollback()
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )
        publication = authority.publication
        schedule = authority.schedule

        plan = await CanonicalRepeatPlanner(self.session).plan_next(
            safe_publication_id,
            after=after,
        )
        if plan is None:
            await self.session.rollback()
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )
        if (
            int(plan.source_publication_id) != int(publication.id)
            or int(plan.source_schedule_entry_id) != int(schedule.id)
            or int(plan.repeat_group_id) != int(authority.repeat_group_id)
            or int(plan.channel_id) != int(publication.channel_id)
            or int(plan.content_item_id) != int(publication.content_item_id)
            or int(plan.content_revision) != int(publication.content_revision)
            or int(plan.repeat_seconds) != int(authority.repeat_seconds)
            or dict(plan.runtime_options) != authority.runtime_options
        ):
            await self.session.rollback()
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="conflict",
                plan=plan,
            )
        if plan.existing_publication_id is not None:
            await self.session.rollback()
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="existing_successor",
                plan=plan,
            )

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if publication_meta is None or schedule_meta is None:
            await self.session.rollback()
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="conflict",
                plan=plan,
            )

        snapshot = _reservation_snapshot(plan)
        publication_existing = publication_meta.get(
            CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY
        )
        schedule_existing = schedule_meta.get(CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY)
        if publication_existing is None and schedule_existing is None:
            publication.meta = {
                **publication_meta,
                CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY: deepcopy(snapshot),
            }
            schedule.meta = {
                **schedule_meta,
                CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY: deepcopy(snapshot),
            }
            await self.session.commit()
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="reserved",
                plan=plan,
            )

        publication_reservation = _mapping(publication_existing)
        schedule_reservation = _mapping(schedule_existing)
        if (
            publication_reservation is not None
            and schedule_reservation is not None
            and publication_reservation == schedule_reservation == snapshot
        ):
            await self.session.rollback()
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="already_reserved",
                plan=plan,
            )

        await self.session.rollback()
        return CanonicalRepeatPlanReservationResult(
            publication_id=safe_publication_id,
            outcome="conflict",
            plan=plan,
        )
