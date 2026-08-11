from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.scheduling import as_utc


class CanonicalPublicationDeliveryRuntimeConflict(RuntimeError):
    """Claimed delivery intent changed before generated runtime could be committed."""


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


def _runtime_options_from_meta(value: Any) -> dict[str, Any] | None:
    meta = _mapping(value)
    if meta is None:
        return None
    raw = meta.get("runtime_options")
    if raw is None:
        return {}
    return _mapping(raw)


def apply_canonical_delivery_success_runtime(
    publication: Publication,
    schedule: ScheduleEntry,
    plan: CanonicalPublicationDeliveryPlan,
    *,
    delivered_at: datetime | None = None,
) -> bool:
    """Apply generated time-autodelete runtime from immutable claimed intent.

    The helper performs no commit. The finalizer owns the transaction so terminal
    Publication/Schedule/Attempt state and generated runtime become durable together.
    Views-based autodelete uses its separate indexed producer state and is not mutated
    here.
    """

    current = as_utc(delivered_at or datetime.now(timezone.utc))
    if (
        int(publication.id) != int(plan.publication_id)
        or int(schedule.id) != int(plan.schedule_entry_id)
        or int(publication.schedule_entry_id or 0) != int(plan.schedule_entry_id)
        or int(publication.channel_id) != int(plan.channel_id)
        or int(schedule.channel_id) != int(plan.channel_id)
        or int(publication.content_item_id) != int(plan.content_item_id)
        or int(schedule.content_item_id) != int(plan.content_item_id)
        or int(publication.content_revision) != int(plan.content_revision)
        or int(schedule.content_revision) != int(plan.content_revision)
        or as_utc(schedule.scheduled_at) != as_utc(plan.scheduled_at)
    ):
        raise CanonicalPublicationDeliveryRuntimeConflict(
            "canonical delivery identity changed"
        )

    try:
        planned_options = plan.runtime_options()
        planned_repeat_rule = plan.repeat_rule()
    except (TypeError, ValueError) as exc:
        raise CanonicalPublicationDeliveryRuntimeConflict(
            "canonical delivery snapshot is invalid"
        ) from exc

    publication_options = _runtime_options_from_meta(publication.meta)
    schedule_options = _runtime_options_from_meta(schedule.meta)
    current_repeat_rule = _mapping(schedule.repeat_rule)
    publication_meta = _mapping(publication.meta)
    if (
        publication_options is None
        or schedule_options is None
        or publication_meta is None
        or current_repeat_rule is None
        or AUTODELETE_RUNTIME_META_KEY in publication_meta
        or publication_options != planned_options
        or schedule_options != planned_options
        or current_repeat_rule != planned_repeat_rule
    ):
        raise CanonicalPublicationDeliveryRuntimeConflict(
            "canonical delivery intent changed"
        )

    seconds = _positive_int(planned_options.get("autodelete_seconds"))
    if seconds is None:
        return False

    repeat_enabled = planned_repeat_rule.get("enabled") is True
    repeat_seconds = _positive_int(planned_repeat_rule.get("seconds"))
    if repeat_enabled and repeat_seconds == seconds:
        due_at = as_utc(plan.scheduled_at) + timedelta(seconds=repeat_seconds)
    else:
        due_at = current + timedelta(seconds=seconds)

    new_meta = deepcopy(publication_meta)
    new_meta[AUTODELETE_RUNTIME_META_KEY] = {
        "deleted": False,
        "effective_seconds": seconds,
        "scheduled_at": due_at.isoformat(),
    }
    publication.meta = new_meta
    return True
