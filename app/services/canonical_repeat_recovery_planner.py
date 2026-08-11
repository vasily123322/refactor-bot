from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.scheduling import as_utc, compute_next_repeat_time


@dataclass(frozen=True, slots=True)
class CanonicalRepeatRecoveryPlan:
    source_publication_id: int
    source_schedule_entry_id: int
    repeat_group_id: int
    channel_id: int
    content_item_id: int
    content_revision: int
    repeat_seconds: int
    scheduled_at: datetime
    runtime_options: dict[str, Any]
    existing_publication_id: int | None = None
    existing_schedule_entry_id: int | None = None


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _runtime_options(
    publication_meta: Mapping[str, Any],
    schedule_meta: Mapping[str, Any],
) -> dict[str, Any] | None:
    publication_value = publication_meta.get("runtime_options")
    schedule_value = schedule_meta.get("runtime_options")
    if publication_value is None and schedule_value is None:
        return {}
    publication_options = _mapping(publication_value)
    schedule_options = _mapping(schedule_value)
    if publication_options is None or schedule_options is None:
        return None
    if publication_options != schedule_options:
        return None
    return deepcopy(publication_options)


class CanonicalRepeatRecoveryPlanner:
    """Pure canonical proof for recovering one overdue unsent repeat occurrence.

    This planner intentionally targets the recovery state that the successful planner
    rejects: Publication queued, ScheduleEntry pending, no delivery attempt/evidence.
    It never reads PostTask and never writes domain or transport rows.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _source(
        self,
        publication_id: int,
    ) -> tuple[Publication, ScheduleEntry, ContentItem, ContentRevision] | None:
        return (
            await self.session.execute(
                select(Publication, ScheduleEntry, ContentItem, ContentRevision)
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                    ),
                )
                .join(
                    ContentItem,
                    and_(
                        ContentItem.id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                        ContentItem.kind == "post",
                    ),
                )
                .join(
                    ContentRevision,
                    and_(
                        ContentRevision.content_item_id == Publication.content_item_id,
                        ContentRevision.revision == Publication.content_revision,
                    ),
                )
                .where(
                    Publication.id == int(publication_id),
                    Publication.status == "queued",
                    ScheduleEntry.status == "pending",
                )
            )
        ).one_or_none()

    async def _has_attempt(self, publication_id: int) -> bool:
        attempt_id = (
            await self.session.execute(
                select(PublicationAttempt.id)
                .where(PublicationAttempt.publication_id == int(publication_id))
                .limit(1)
            )
        ).scalar_one_or_none()
        return attempt_id is not None

    async def _existing_successor(
        self,
        *,
        source_publication_id: int,
        repeat_group_id: int,
        channel_id: int,
        content_item_id: int,
        content_revision: int,
        scheduled_at: datetime,
    ) -> tuple[int, int] | None | bool:
        rows = (
            await self.session.execute(
                select(Publication.id, ScheduleEntry.id)
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                    ),
                )
                .where(
                    Publication.id != int(source_publication_id),
                    Publication.channel_id == int(channel_id),
                    Publication.content_item_id == int(content_item_id),
                    Publication.content_revision == int(content_revision),
                    ScheduleEntry.scheduled_at == scheduled_at,
                    ScheduleEntry.meta["repeat_group_id"].as_integer()
                    == int(repeat_group_id),
                )
                .order_by(Publication.id.asc())
                .limit(2)
            )
        ).all()
        if len(rows) > 1:
            return False
        if not rows:
            return None
        publication_id, schedule_id = rows[0]
        return int(publication_id), int(schedule_id)

    async def plan_recovery(
        self,
        publication_id: int,
        *,
        after: datetime | None = None,
    ) -> CanonicalRepeatRecoveryPlan | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        source = await self._source(safe_publication_id)
        if source is None:
            return None
        publication, schedule, item, revision = source

        if (
            int(publication.attempt_count or 0) != 0
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
            or await self._has_attempt(safe_publication_id)
        ):
            return None

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        repeat_rule = _mapping(schedule.repeat_rule)
        if publication_meta is None or schedule_meta is None or repeat_rule is None:
            return None

        publication_group = _positive_int(publication_meta.get("repeat_group_id"))
        schedule_group = _positive_int(schedule_meta.get("repeat_group_id"))
        if publication_group is None or publication_group != schedule_group:
            return None
        if repeat_rule.get("enabled") is not True:
            return None
        repeat_seconds = _positive_int(repeat_rule.get("seconds"))
        if repeat_seconds is None:
            return None

        runtime_options = _runtime_options(publication_meta, schedule_meta)
        if runtime_options is None:
            return None

        source_scheduled_at = as_utc(schedule.scheduled_at)
        current = as_utc(after or datetime.now(timezone.utc))
        if source_scheduled_at > current:
            return None
        next_scheduled_at = compute_next_repeat_time(
            source_scheduled_at,
            repeat_seconds,
            current,
        )

        existing = await self._existing_successor(
            source_publication_id=int(publication.id),
            repeat_group_id=publication_group,
            channel_id=int(publication.channel_id),
            content_item_id=int(item.id),
            content_revision=int(revision.revision),
            scheduled_at=next_scheduled_at,
        )
        if existing is False:
            return None
        existing_publication_id = None
        existing_schedule_entry_id = None
        if existing is not None:
            existing_publication_id, existing_schedule_entry_id = existing

        return CanonicalRepeatRecoveryPlan(
            source_publication_id=int(publication.id),
            source_schedule_entry_id=int(schedule.id),
            repeat_group_id=publication_group,
            channel_id=int(publication.channel_id),
            content_item_id=int(item.id),
            content_revision=int(revision.revision),
            repeat_seconds=repeat_seconds,
            scheduled_at=next_scheduled_at,
            runtime_options=deepcopy(runtime_options),
            existing_publication_id=existing_publication_id,
            existing_schedule_entry_id=existing_schedule_entry_id,
        )
