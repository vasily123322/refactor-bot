from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.scheduling import as_utc
from app.services.telegram_results import normalize_telegram_message_ids


_SUPPORTED_RUNTIME_KEYS = frozenset(
    {
        "silent",
        "pin_on",
        "forward_to",
        "autodelete_seconds",
        "autodelete_effective_seconds",
        "autodelete_views",
        "autodelete_report",
    }
)


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
    if not set(publication_options).issubset(_SUPPORTED_RUNTIME_KEYS):
        return None
    return deepcopy(publication_options)


def _safe_nonrepeat(schedule: ScheduleEntry) -> bool:
    raw_rule = schedule.repeat_rule
    if raw_rule is not None and not isinstance(raw_rule, Mapping):
        return False
    rule = _mapping(raw_rule) or {}
    enabled = rule.get("enabled")
    return enabled is None or enabled is False


def _time_only_seconds(options: Mapping[str, Any]) -> int | None:
    raw_views = options.get("autodelete_views")
    parsed_views = _positive_int(raw_views)
    if raw_views not in (None, False, 0, "0", "") and parsed_views is None:
        return None
    if parsed_views is not None:
        return None

    report = options.get("autodelete_report")
    if report is not None and not isinstance(report, bool):
        return None

    effective = _positive_int(options.get("autodelete_effective_seconds"))
    base = _positive_int(options.get("autodelete_seconds"))
    return effective or base


@dataclass(frozen=True, slots=True)
class CanonicalPublicationAutodeleteRuntimePlan:
    publication_id: int
    effective_seconds: int
    scheduled_at: datetime
    state: dict[str, Any]
    existing: bool = False


class CanonicalPublicationAutodeleteRuntimePlanner:
    """Pure canonical-only non-repeat time-based autodelete runtime proof.

    Runtime is derived only from a terminal delivery attempt that was itself produced by
    canonical delivery authority. Physical PostTask retirement alone is not sufficient:
    a legacy-origin attempt must never become canonical autodelete authority merely
    because retention later clears ``legacy_post_task_id``.

    The due instant matches historical non-repeat behavior by anchoring to the durable
    primary provider completion time, ``PublicationAttempt.finished_at``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        publication_id: int,
    ) -> CanonicalPublicationAutodeleteRuntimePlan | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        row = (
            await self.session.execute(
                select(Publication, ScheduleEntry, PublicationAttempt)
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
                    PublicationAttempt,
                    and_(
                        PublicationAttempt.publication_id == Publication.id,
                        PublicationAttempt.attempt == Publication.attempt_count,
                    ),
                )
                .where(
                    Publication.id == safe_publication_id,
                    Publication.legacy_post_task_id.is_(None),
                    Publication.status == "published",
                    ScheduleEntry.status == "completed",
                    PublicationAttempt.status == "published",
                    PublicationAttempt.finished_at.is_not(None),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        publication, schedule, attempt = row
        if not isinstance(attempt.meta, Mapping) or dict(attempt.meta).get(
            "canonical_delivery"
        ) is not True:
            return None
        if not _safe_nonrepeat(schedule):
            return None

        publication_ids = normalize_telegram_message_ids(publication.telegram_message_ids)
        attempt_ids = normalize_telegram_message_ids(attempt.telegram_message_ids)
        if not publication_ids or publication_ids != attempt_ids:
            return None

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if publication_meta is None or schedule_meta is None:
            return None
        options = _runtime_options(publication_meta, schedule_meta)
        if options is None:
            return None
        seconds = _time_only_seconds(options)
        if seconds is None:
            return None

        delivered_at = as_utc(attempt.finished_at)
        due_at = delivered_at + timedelta(seconds=seconds)
        desired_state: dict[str, Any] = {
            "deleted": False,
            "effective_seconds": seconds,
            "scheduled_at": due_at.isoformat(),
        }

        existing_raw = publication_meta.get(AUTODELETE_RUNTIME_META_KEY)
        if existing_raw is None:
            return CanonicalPublicationAutodeleteRuntimePlan(
                publication_id=safe_publication_id,
                effective_seconds=seconds,
                scheduled_at=due_at,
                state=desired_state,
                existing=False,
            )
        existing = _mapping(existing_raw)
        if existing is None or existing != desired_state:
            return None
        return CanonicalPublicationAutodeleteRuntimePlan(
            publication_id=safe_publication_id,
            effective_seconds=seconds,
            scheduled_at=due_at,
            state=deepcopy(desired_state),
            existing=True,
        )
