from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, ScheduleEntry


_MAX_DB_ID = (1 << 63) - 1


def _db_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed <= 0 or parsed > _MAX_DB_ID:
        return None
    return parsed


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _safe_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


async def _canonical_repeat_root(
    session: AsyncSession,
    *,
    group_id: int,
    channel_id: int,
    content_item_id: int,
    content_revision: int,
) -> tuple[Publication, ScheduleEntry] | None:
    linked = (
        await session.execute(
            select(Publication, ScheduleEntry)
            .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
            .where(
                Publication.legacy_post_task_id == int(group_id),
                Publication.channel_id == int(channel_id),
                Publication.content_item_id == int(content_item_id),
                Publication.content_revision == int(content_revision),
                ScheduleEntry.channel_id == int(channel_id),
                ScheduleEntry.content_item_id == int(content_item_id),
                ScheduleEntry.content_revision == int(content_revision),
            )
        )
    ).one_or_none()
    if linked is not None:
        return linked

    return (
        await session.execute(
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
            .where(
                Publication.channel_id == int(channel_id),
                Publication.content_item_id == int(content_item_id),
                Publication.content_revision == int(content_revision),
                ScheduleEntry.meta["repeat_group_id"].as_integer() == int(group_id),
            )
            .order_by(Publication.id.asc())
            .limit(1)
        )
    ).one_or_none()


async def canonical_repeat_runtime_intent(
    session: AsyncSession,
    *,
    payload: Mapping[str, Any],
    channel_id: int,
    task_id: int,
    content_item_id: int,
    content_revision: int,
) -> dict[str, Any] | None:
    """Return exact canonical root runtime intent for one proven repeat child.

    This is intentionally fail-closed. Runtime intent is copied only when canonical
    Publication/Schedule provenance agrees with the repeat child transport facts. The
    legacy scheduler remains the executor; this helper only proves metadata lineage.
    """
    data = dict(payload)
    if not bool(data.get("repeat_on", False)):
        return None
    group_id = _db_id(data.get("repeat_group_id"))
    if group_id is None or group_id == int(task_id):
        return None
    repeat_seconds = _positive_int(data.get("repeat_seconds"))
    if repeat_seconds is None:
        return None

    root = await _canonical_repeat_root(
        session,
        group_id=group_id,
        channel_id=int(channel_id),
        content_item_id=int(content_item_id),
        content_revision=int(content_revision),
    )
    if root is None:
        return None
    publication, schedule = root

    publication_meta = _safe_mapping(publication.meta)
    schedule_meta = _safe_mapping(schedule.meta)
    repeat_rule = _safe_mapping(schedule.repeat_rule)
    if publication_meta is None or schedule_meta is None or repeat_rule is None:
        return None
    if _db_id(publication_meta.get("repeat_group_id")) != group_id:
        return None
    if _db_id(schedule_meta.get("repeat_group_id")) != group_id:
        return None
    if repeat_rule.get("enabled") is not True:
        return None
    if _positive_int(repeat_rule.get("seconds")) != repeat_seconds:
        return None

    publication_options = publication_meta.get("runtime_options")
    schedule_options = schedule_meta.get("runtime_options")
    if publication_options is None and schedule_options is None:
        return {}
    publication_intent = _safe_mapping(publication_options)
    schedule_intent = _safe_mapping(schedule_options)
    if publication_intent is None or schedule_intent is None:
        return None
    if publication_intent != schedule_intent:
        return None

    for key, value in publication_intent.items():
        if key not in data or data.get(key) != value:
            return None
    return deepcopy(publication_intent)
