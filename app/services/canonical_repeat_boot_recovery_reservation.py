from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_boot_recovery_planner import (
    CanonicalRepeatBootRecoveryPlan,
    CanonicalRepeatBootRecoveryPlanner,
    MAX_BOOT_GROUP_SOURCES,
)
from app.services.scheduling import as_utc


CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY = (
    "canonical_repeat_boot_recovery_reservation"
)


@dataclass(frozen=True, slots=True)
class CanonicalRepeatBootRecoveryReservationResult:
    source_publication_ids: tuple[int, ...]
    outcome: Literal[
        "reserved",
        "already_reserved",
        "existing_successor",
        "ineligible",
        "conflict",
    ]
    plan: CanonicalRepeatBootRecoveryPlan | None = None


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _source_ids(values: Sequence[int]) -> tuple[int, ...] | None:
    if isinstance(values, (str, bytes)):
        return None
    if not values or len(values) > MAX_BOOT_GROUP_SOURCES:
        return None
    parsed: list[int] = []
    for raw in values:
        if isinstance(raw, bool):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if value <= 0:
            return None
        parsed.append(value)
    if len(set(parsed)) != len(parsed):
        return None
    return tuple(parsed)


def _snapshot(plan: CanonicalRepeatBootRecoveryPlan) -> dict[str, Any]:
    return {
        "version": 1,
        "sources": [
            {
                "publication_id": int(source.publication_id),
                "schedule_entry_id": int(source.schedule_entry_id),
                "scheduled_at": as_utc(source.scheduled_at).isoformat(),
            }
            for source in plan.sources
        ],
        "anchor_publication_id": int(plan.anchor_publication_id),
        "anchor_schedule_entry_id": int(plan.anchor_schedule_entry_id),
        "repeat_group_id": int(plan.repeat_group_id),
        "channel_id": int(plan.channel_id),
        "content_item_id": int(plan.content_item_id),
        "content_revision": int(plan.content_revision),
        "repeat_seconds": int(plan.repeat_seconds),
        "scheduled_at": as_utc(plan.scheduled_at).isoformat(),
        "runtime_options": deepcopy(plan.runtime_options),
    }


class CanonicalRepeatBootRecoveryReservationService:
    """Reserve one deterministic canonical plan across every source in a boot group.

    Source Publication/Schedule pairs are locked first in stable Publication-id order.
    The pure group planner is then re-evaluated inside the same transaction. A single
    deterministic snapshot is written to all 2N metadata documents only when every
    reservation slot is empty. Existing one-sided or divergent metadata is never
    repaired or overwritten.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_sources(
        self,
        source_ids: tuple[int, ...],
    ) -> dict[int, tuple[Publication, ScheduleEntry]] | None:
        rows = (
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
                .where(Publication.id.in_(source_ids))
                .order_by(Publication.id.asc())
                .with_for_update()
            )
        ).all()
        if len(rows) != len(source_ids):
            return None
        locked = {int(publication.id): (publication, schedule) for publication, schedule in rows}
        if len(locked) != len(source_ids):
            return None
        return locked

    async def reserve_group(
        self,
        source_publication_ids: Sequence[int],
        *,
        after: datetime | None = None,
    ) -> CanonicalRepeatBootRecoveryReservationResult:
        source_ids = _source_ids(source_publication_ids)
        if source_ids is None:
            return CanonicalRepeatBootRecoveryReservationResult((), "ineligible")

        locked = await self._lock_sources(source_ids)
        if locked is None:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryReservationResult(source_ids, "ineligible")

        plan = await CanonicalRepeatBootRecoveryPlanner(self.session).plan_group(
            source_ids,
            after=after,
        )
        if plan is None:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryReservationResult(source_ids, "ineligible")
        if tuple(source.publication_id for source in plan.sources) != source_ids:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryReservationResult(
                source_ids,
                "conflict",
                plan,
            )

        for source in plan.sources:
            locked_pair = locked.get(int(source.publication_id))
            if locked_pair is None:
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryReservationResult(
                    source_ids,
                    "conflict",
                    plan,
                )
            publication, schedule = locked_pair
            if (
                int(schedule.id) != int(source.schedule_entry_id)
                or as_utc(schedule.scheduled_at) != as_utc(source.scheduled_at)
            ):
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryReservationResult(
                    source_ids,
                    "conflict",
                    plan,
                )

        if plan.existing_publication_id is not None:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryReservationResult(
                source_ids,
                "existing_successor",
                plan,
            )

        snapshot = _snapshot(plan)
        meta_pairs: list[
            tuple[Publication, ScheduleEntry, dict[str, Any], dict[str, Any]]
        ] = []
        existing_values: list[Any] = []
        for source in plan.sources:
            publication, schedule = locked[int(source.publication_id)]
            publication_meta = _mapping(publication.meta)
            schedule_meta = _mapping(schedule.meta)
            if publication_meta is None or schedule_meta is None:
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryReservationResult(
                    source_ids,
                    "conflict",
                    plan,
                )
            meta_pairs.append((publication, schedule, publication_meta, schedule_meta))
            existing_values.extend(
                [
                    publication_meta.get(
                        CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY
                    ),
                    schedule_meta.get(CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY),
                ]
            )

        if all(value is None for value in existing_values):
            for publication, schedule, publication_meta, schedule_meta in meta_pairs:
                publication.meta = {
                    **publication_meta,
                    CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY: deepcopy(snapshot),
                }
                schedule.meta = {
                    **schedule_meta,
                    CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY: deepcopy(snapshot),
                }
            await self.session.commit()
            return CanonicalRepeatBootRecoveryReservationResult(
                source_ids,
                "reserved",
                plan,
            )

        reservations = [_mapping(value) for value in existing_values]
        if all(reservation == snapshot for reservation in reservations):
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryReservationResult(
                source_ids,
                "already_reserved",
                plan,
            )

        await self.session.rollback()
        return CanonicalRepeatBootRecoveryReservationResult(
            source_ids,
            "conflict",
            plan,
        )
