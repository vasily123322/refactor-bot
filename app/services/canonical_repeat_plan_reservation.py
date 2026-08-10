from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, ScheduleEntry
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
    """Persist one deterministic canonical repeat plan on its terminal source rows.

    This stage deliberately does not create a successor ScheduleEntry, Publication or
    PostTask. Source Publication/Schedule rows are locked first, the pure planner is
    re-evaluated inside that transaction, and the same deterministic snapshot is then
    written to both metadata documents. Existing canonical metadata is never repaired
    from one side or overwritten with a different reservation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_source(
        self,
        publication_id: int,
    ) -> tuple[Publication, ScheduleEntry] | None:
        return (
            await self.session.execute(
                select(Publication, ScheduleEntry)
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                    ),
                )
                .where(Publication.id == int(publication_id))
                .with_for_update()
            )
        ).one_or_none()

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

        locked = await self._lock_source(safe_publication_id)
        if locked is None:
            await self.session.rollback()
            return CanonicalRepeatPlanReservationResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )
        publication, schedule = locked

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
