from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_repeat_recovery_planner import (
    CanonicalRepeatRecoveryPlan,
    CanonicalRepeatRecoveryPlanner,
)
from app.services.scheduling import as_utc


MAX_BOOT_GROUP_SOURCES = 100


@dataclass(frozen=True, slots=True)
class CanonicalRepeatBootRecoverySource:
    publication_id: int
    schedule_entry_id: int
    scheduled_at: datetime


@dataclass(frozen=True, slots=True)
class CanonicalRepeatBootRecoveryPlan:
    sources: tuple[CanonicalRepeatBootRecoverySource, ...]
    anchor_publication_id: int
    anchor_schedule_entry_id: int
    repeat_group_id: int
    channel_id: int
    content_item_id: int
    content_revision: int
    repeat_seconds: int
    scheduled_at: datetime
    runtime_options: dict[str, Any]
    existing_publication_id: int | None = None
    existing_schedule_entry_id: int | None = None


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


def _signature(plan: CanonicalRepeatRecoveryPlan) -> tuple[Any, ...]:
    return (
        int(plan.repeat_group_id),
        int(plan.channel_id),
        int(plan.content_item_id),
        int(plan.content_revision),
        int(plan.repeat_seconds),
        plan.runtime_options,
        as_utc(plan.scheduled_at),
        plan.existing_publication_id,
        plan.existing_schedule_entry_id,
    )


class CanonicalRepeatBootRecoveryPlanner:
    """Pure group proof for the legacy scheduler's one-time boot repeat cleanup.

    The caller supplies canonical Publication ids in the same order as the scheduler's
    selected batch. Each source must independently satisfy the strict canonical overdue
    recovery proof. The group then proves that all sources describe one coherent repeat
    series and calculate the same exact future slot. No PostTask rows are read or written.

    This stage deliberately plans one repeat group at a time. Persisting skipped source
    audit, materializing one successor, grouping a mixed scheduler batch, and runtime
    cutover are later stages.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan_group(
        self,
        source_publication_ids: Sequence[int],
        *,
        after: datetime | None = None,
    ) -> CanonicalRepeatBootRecoveryPlan | None:
        source_ids = _source_ids(source_publication_ids)
        if source_ids is None:
            return None

        current = as_utc(after or datetime.now(timezone.utc))
        planner = CanonicalRepeatRecoveryPlanner(self.session)
        source_plans: list[CanonicalRepeatRecoveryPlan] = []
        for publication_id in source_ids:
            plan = await planner.plan_recovery(publication_id, after=current)
            if plan is None:
                return None
            source_plans.append(plan)

        anchor = source_plans[0]
        anchor_signature = _signature(anchor)
        previous_at: datetime | None = None
        sources: list[CanonicalRepeatBootRecoverySource] = []
        for plan in source_plans:
            source_at = as_utc(plan.source_scheduled_at)
            # Legacy scheduler selects due tasks by scheduled_at ascending. Preserve
            # that caller-order contract and fail closed if a future runtime passes an
            # order that could choose a different first source as the group anchor.
            if previous_at is not None and source_at < previous_at:
                return None
            previous_at = source_at

            if _signature(plan) != anchor_signature:
                # This simultaneously proves one group/content/revision/runtime intent,
                # one interval/phase and one exact successor (if already present).
                return None

            sources.append(
                CanonicalRepeatBootRecoverySource(
                    publication_id=int(plan.source_publication_id),
                    schedule_entry_id=int(plan.source_schedule_entry_id),
                    scheduled_at=source_at,
                )
            )

        return CanonicalRepeatBootRecoveryPlan(
            sources=tuple(sources),
            anchor_publication_id=int(anchor.source_publication_id),
            anchor_schedule_entry_id=int(anchor.source_schedule_entry_id),
            repeat_group_id=int(anchor.repeat_group_id),
            channel_id=int(anchor.channel_id),
            content_item_id=int(anchor.content_item_id),
            content_revision=int(anchor.content_revision),
            repeat_seconds=int(anchor.repeat_seconds),
            scheduled_at=as_utc(anchor.scheduled_at),
            runtime_options=deepcopy(anchor.runtime_options),
            existing_publication_id=anchor.existing_publication_id,
            existing_schedule_entry_id=anchor.existing_schedule_entry_id,
        )
