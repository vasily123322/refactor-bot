from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry


_REPEAT_CONTROL_META_KEY = "canonical_repeat_control"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _group_from_task(task: PostTask) -> int | None:
    payload = task.payload if isinstance(task.payload, Mapping) else None
    if payload is None:
        return None
    raw = payload.get("repeat_group_id")
    if raw is None and payload.get("repeat_on") is True:
        raw = task.id
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


@dataclass(frozen=True, slots=True)
class CanonicalRepeatSeriesStopResult:
    repeat_group_id: int
    outcome: Literal["stopped", "not_found", "ineligible", "conflict"]
    canonical_schedules_disabled: int = 0
    pending_transports_disabled: int = 0


class CanonicalRepeatSeriesControlService:
    """Stop future repeat continuation across canonical and compatibility authority.

    The historical content-plan control rewrote only pending ``PostTask`` payloads. That
    is insufficient after canonical repeat retirement because a completed canonical
    source may create its successor from ``ScheduleEntry.repeat_rule`` even when its
    compatibility transport has already been physically retired.

    This control locks every owned canonical schedule currently carrying the durable
    repeat-group identity, disables its repeat rule, and then demotes all owned pending
    compatibility transports for the same group in the same transaction. Canonical
    continuation primitives re-lock and prove the source rule before reserve/verify/
    materialize, so ``enabled=False`` serializes against a selected or pre-reserved
    continuation and makes it fail closed before creating another successor.

    The current pending occurrence is intentionally not cancelled: it remains scheduled
    to publish once, matching the historical ``repeat off`` user behavior. Only future
    successor creation is disabled.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def stop_owned_series(
        self,
        *,
        repeat_group_id: int,
        tg_user_id: int,
        at: datetime | None = None,
    ) -> CanonicalRepeatSeriesStopResult:
        try:
            group_id = int(repeat_group_id)
            user_id = int(tg_user_id)
        except (TypeError, ValueError, OverflowError):
            return CanonicalRepeatSeriesStopResult(0, "ineligible")
        if group_id <= 0 or user_id <= 0:
            return CanonicalRepeatSeriesStopResult(group_id, "ineligible")
        stopped_at = (at or _utc_now()).astimezone(timezone.utc)

        try:
            canonical_rows = (
                await self.session.execute(
                    select(ScheduleEntry, Publication)
                    .join(
                        Publication,
                        and_(
                            Publication.schedule_entry_id == ScheduleEntry.id,
                            Publication.channel_id == ScheduleEntry.channel_id,
                            Publication.content_item_id == ScheduleEntry.content_item_id,
                            Publication.content_revision == ScheduleEntry.content_revision,
                        ),
                    )
                    .join(Channel, Channel.id == ScheduleEntry.channel_id)
                    .join(Client, Client.id == Channel.owner_id)
                    .where(
                        Client.tg_user_id == user_id,
                        ScheduleEntry.meta["repeat_group_id"].as_integer() == group_id,
                    )
                    .order_by(ScheduleEntry.id.asc())
                    .with_for_update()
                )
            ).all()

            tasks = list(
                (
                    await self.session.execute(
                        select(PostTask)
                        .join(Channel, Channel.id == PostTask.channel_id)
                        .join(Client, Client.id == Channel.owner_id)
                        .where(
                            Client.tg_user_id == user_id,
                            PostTask.status == "pending",
                            or_(
                                PostTask.id == group_id,
                                PostTask.payload["repeat_group_id"].as_integer()
                                == group_id,
                            ),
                        )
                        .order_by(PostTask.id.asc())
                        .with_for_update()
                    )
                ).scalars().all()
            )

            if not canonical_rows and not tasks:
                await self.session.rollback()
                return CanonicalRepeatSeriesStopResult(group_id, "not_found")

            canonical_by_schedule: dict[int, tuple[ScheduleEntry, Publication]] = {
                int(schedule.id): (schedule, publication)
                for schedule, publication in canonical_rows
            }

            # A continuation that committed a successor just before this control acquired
            # the source lock may only become visible through the compatibility transport
            # query above. Lock and include that linked schedule as a second-pass fence.
            for task in tasks:
                linked = (
                    await self.session.execute(
                        select(ScheduleEntry, Publication)
                        .join(
                            Publication,
                            and_(
                                Publication.schedule_entry_id == ScheduleEntry.id,
                                Publication.channel_id == ScheduleEntry.channel_id,
                                Publication.content_item_id == ScheduleEntry.content_item_id,
                                Publication.content_revision == ScheduleEntry.content_revision,
                            ),
                        )
                        .where(Publication.legacy_post_task_id == int(task.id))
                        .with_for_update()
                    )
                ).one_or_none()
                if linked is not None:
                    schedule, publication = linked
                    schedule_meta = schedule.meta if isinstance(schedule.meta, Mapping) else None
                    if schedule_meta is None:
                        await self.session.rollback()
                        return CanonicalRepeatSeriesStopResult(group_id, "conflict")
                    raw_group = schedule_meta.get("repeat_group_id")
                    try:
                        linked_group = int(raw_group)
                    except (TypeError, ValueError, OverflowError):
                        linked_group = 0
                    if linked_group != group_id:
                        await self.session.rollback()
                        return CanonicalRepeatSeriesStopResult(group_id, "conflict")
                    canonical_by_schedule[int(schedule.id)] = (schedule, publication)

            canonical_disabled = 0
            control_marker = {
                "version": 1,
                "disabled": True,
                "reason": "content_plan_repeat_off",
                "repeat_group_id": group_id,
                "disabled_by_tg_user_id": user_id,
                "disabled_at": stopped_at.isoformat(),
            }
            for schedule, publication in canonical_by_schedule.values():
                raw_rule = schedule.repeat_rule
                if raw_rule is not None and not isinstance(raw_rule, Mapping):
                    await self.session.rollback()
                    return CanonicalRepeatSeriesStopResult(group_id, "conflict")
                rule = deepcopy(dict(raw_rule or {}))
                if rule.get("enabled") is True:
                    rule["enabled"] = False
                    schedule.repeat_rule = rule
                    canonical_disabled += 1

                schedule_meta = schedule.meta
                publication_meta = publication.meta
                if not isinstance(schedule_meta, Mapping) or not isinstance(
                    publication_meta, Mapping
                ):
                    await self.session.rollback()
                    return CanonicalRepeatSeriesStopResult(group_id, "conflict")
                schedule.meta = {
                    **deepcopy(dict(schedule_meta)),
                    _REPEAT_CONTROL_META_KEY: deepcopy(control_marker),
                }
                publication.meta = {
                    **deepcopy(dict(publication_meta)),
                    _REPEAT_CONTROL_META_KEY: deepcopy(control_marker),
                }

            transport_disabled = 0
            for task in tasks:
                if _group_from_task(task) != group_id:
                    await self.session.rollback()
                    return CanonicalRepeatSeriesStopResult(group_id, "conflict")
                if not isinstance(task.payload, Mapping):
                    await self.session.rollback()
                    return CanonicalRepeatSeriesStopResult(group_id, "conflict")
                payload = deepcopy(dict(task.payload))
                changed = payload.get("repeat_on") is True or "repeat_seconds" in payload
                payload["repeat_on"] = False
                payload.pop("repeat_seconds", None)
                task.payload = payload
                if changed:
                    transport_disabled += 1

            await self.session.commit()
            return CanonicalRepeatSeriesStopResult(
                group_id,
                "stopped",
                canonical_schedules_disabled=canonical_disabled,
                pending_transports_disabled=transport_disabled,
            )
        except Exception:
            await self.session.rollback()
            raise