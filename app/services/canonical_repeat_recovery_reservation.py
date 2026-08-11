from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_recovery_planner import (
    CanonicalRepeatRecoveryPlan,
    CanonicalRepeatRecoveryPlanner,
)
from app.services.scheduling import as_utc


CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY = (
    "canonical_repeat_recovery_reservation"
)


@dataclass(frozen=True, slots=True)
class CanonicalRepeatRecoveryReservationResult:
    publication_id: int
    outcome: Literal[
        "reserved",
        "already_reserved",
        "existing_successor",
        "ineligible",
        "conflict",
    ]
    plan: CanonicalRepeatRecoveryPlan | None = None


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _snapshot(plan: CanonicalRepeatRecoveryPlan) -> dict[str, Any]:
    return {
        "version": 1,
        "source_publication_id": int(plan.source_publication_id),
        "source_schedule_entry_id": int(plan.source_schedule_entry_id),
        "source_scheduled_at": as_utc(plan.source_scheduled_at).isoformat(),
        "repeat_group_id": int(plan.repeat_group_id),
        "channel_id": int(plan.channel_id),
        "content_item_id": int(plan.content_item_id),
        "content_revision": int(plan.content_revision),
        "repeat_seconds": int(plan.repeat_seconds),
        "scheduled_at": as_utc(plan.scheduled_at).isoformat(),
        "runtime_options": deepcopy(plan.runtime_options),
    }


class CanonicalRepeatRecoveryReservationService:
    """Persist one deterministic overdue recovery plan on the queued source rows.

    This stage does not skip the source and does not create a successor or PostTask.
    Existing canonical metadata is never repaired from only one side or overwritten
    with a different plan.
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

    async def reserve_recovery(
        self,
        publication_id: int,
        *,
        after: datetime | None = None,
    ) -> CanonicalRepeatRecoveryReservationResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return CanonicalRepeatRecoveryReservationResult(0, "ineligible")
        if safe_publication_id <= 0:
            return CanonicalRepeatRecoveryReservationResult(
                safe_publication_id,
                "ineligible",
            )

        locked = await self._lock_source(safe_publication_id)
        if locked is None:
            await self.session.rollback()
            return CanonicalRepeatRecoveryReservationResult(
                safe_publication_id,
                "ineligible",
            )
        publication, schedule = locked

        plan = await CanonicalRepeatRecoveryPlanner(self.session).plan_recovery(
            safe_publication_id,
            after=after,
        )
        if plan is None:
            await self.session.rollback()
            return CanonicalRepeatRecoveryReservationResult(
                safe_publication_id,
                "ineligible",
            )
        if (
            int(plan.source_publication_id) != int(publication.id)
            or int(plan.source_schedule_entry_id) != int(schedule.id)
            or as_utc(plan.source_scheduled_at) != as_utc(schedule.scheduled_at)
        ):
            await self.session.rollback()
            return CanonicalRepeatRecoveryReservationResult(
                safe_publication_id,
                "conflict",
                plan,
            )
        if plan.existing_publication_id is not None:
            await self.session.rollback()
            return CanonicalRepeatRecoveryReservationResult(
                safe_publication_id,
                "existing_successor",
                plan,
            )

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if publication_meta is None or schedule_meta is None:
            await self.session.rollback()
            return CanonicalRepeatRecoveryReservationResult(
                safe_publication_id,
                "conflict",
                plan,
            )

        snapshot = _snapshot(plan)
        publication_existing = publication_meta.get(
            CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY
        )
        schedule_existing = schedule_meta.get(
            CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY
        )
        if publication_existing is None and schedule_existing is None:
            publication.meta = {
                **publication_meta,
                CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY: deepcopy(snapshot),
            }
            schedule.meta = {
                **schedule_meta,
                CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY: deepcopy(snapshot),
            }
            await self.session.commit()
            return CanonicalRepeatRecoveryReservationResult(
                safe_publication_id,
                "reserved",
                plan,
            )

        publication_reservation = _mapping(publication_existing)
        schedule_reservation = _mapping(schedule_existing)
        if (
            publication_reservation is not None
            and schedule_reservation is not None
            and publication_reservation == schedule_reservation == snapshot
        ):
            await self.session.rollback()
            return CanonicalRepeatRecoveryReservationResult(
                safe_publication_id,
                "already_reserved",
                plan,
            )

        await self.session.rollback()
        return CanonicalRepeatRecoveryReservationResult(
            safe_publication_id,
            "conflict",
            plan,
        )
