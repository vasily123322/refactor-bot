from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.scheduling import as_utc
from app.services.telegram_results import normalize_telegram_message_ids


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


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


def _safe_nonrepeat(schedule: ScheduleEntry) -> bool:
    raw_rule = schedule.repeat_rule
    if raw_rule is not None and not isinstance(raw_rule, Mapping):
        return False
    rule = _mapping(raw_rule) or {}
    enabled = rule.get("enabled")
    return enabled is None or enabled is False


def _runtime_scheduled_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class CanonicalPublicationAutodeleteRuntimePlan:
    publication_id: int
    effective_seconds: int
    scheduled_at: datetime
    state: dict[str, Any]
    existing: bool = False


class CanonicalPublicationAutodeleteRuntimePlanner:
    """Pure canonical-only non-repeat time-based autodelete runtime proof.

    Runtime is derived only from a terminal attempt produced by canonical delivery.
    Physical PostTask retirement alone never transfers this authority.

    Normal live delivery materializes its timer after admin handling, matching the legacy
    scheduling boundary, so an existing durable due time may be later than primary
    provider completion plus the configured duration. This terminal planner accepts such
    runtime only when it is structurally exact, uses the same effective duration, and is
    no earlier than that provider-completion floor.

    If runtime is missing entirely, the planner exposes the conservative floor as a
    reconciliation/backfill proposal; that fallback is not a claim of exact live legacy
    scheduling time.
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
        capability = parse_canonical_publication_delivery_runtime_capability(options)
        if capability is None or not capability.time_autodelete_requested:
            return None
        seconds = int(capability.time_autodelete_seconds or 0)
        if seconds <= 0:
            return None

        delivered_at = as_utc(attempt.finished_at)
        earliest_due = delivered_at + timedelta(seconds=seconds)
        fallback_state: dict[str, Any] = {
            "deleted": False,
            "effective_seconds": seconds,
            "scheduled_at": earliest_due.isoformat(),
        }

        existing_raw = publication_meta.get(AUTODELETE_RUNTIME_META_KEY)
        if existing_raw is None:
            return CanonicalPublicationAutodeleteRuntimePlan(
                publication_id=safe_publication_id,
                effective_seconds=seconds,
                scheduled_at=earliest_due,
                state=fallback_state,
                existing=False,
            )

        existing = _mapping(existing_raw)
        if existing is None or set(existing) != {
            "deleted",
            "effective_seconds",
            "scheduled_at",
        }:
            return None
        if existing.get("deleted") is not False:
            return None
        raw_seconds = existing.get("effective_seconds")
        if isinstance(raw_seconds, bool):
            return None
        try:
            existing_seconds = int(raw_seconds)
        except (TypeError, ValueError, OverflowError):
            return None
        existing_due = _runtime_scheduled_at(existing.get("scheduled_at"))
        if (
            existing_seconds != seconds
            or existing_due is None
            or existing_due < earliest_due
        ):
            return None

        return CanonicalPublicationAutodeleteRuntimePlan(
            publication_id=safe_publication_id,
            effective_seconds=seconds,
            scheduled_at=existing_due,
            state=deepcopy(existing),
            existing=True,
        )
