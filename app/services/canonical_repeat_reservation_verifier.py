from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_plan_reservation import (
    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY,
)
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatReservationVerification:
    source_publication_id: int
    outcome: Literal["matched", "pending", "ineligible", "conflict"]
    successor_publication_id: int | None = None
    successor_schedule_entry_id: int | None = None
    successor_legacy_post_task_id: int | None = None


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


def _scheduled_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 128:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return as_utc(parsed)


def _runtime_options(meta: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = meta.get("runtime_options")
    if raw is None:
        return {}
    return _mapping(raw)


class CanonicalRepeatReservationVerifier:
    """Read-only proof that a legacy-created successor fulfills a reservation."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def verify(
        self,
        source_publication_id: int,
    ) -> CanonicalRepeatReservationVerification:
        try:
            safe_source_id = int(source_publication_id)
        except (TypeError, ValueError, OverflowError):
            return CanonicalRepeatReservationVerification(0, "ineligible")
        if safe_source_id <= 0:
            return CanonicalRepeatReservationVerification(safe_source_id, "ineligible")

        source = (
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
                .where(
                    Publication.id == safe_source_id,
                    Publication.status == "published",
                    ScheduleEntry.status == "completed",
                )
            )
        ).one_or_none()
        if source is None:
            return CanonicalRepeatReservationVerification(safe_source_id, "ineligible")
        publication, schedule = source

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if publication_meta is None or schedule_meta is None:
            return CanonicalRepeatReservationVerification(safe_source_id, "conflict")
        publication_reservation = _mapping(
            publication_meta.get(CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY)
        )
        schedule_reservation = _mapping(
            schedule_meta.get(CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY)
        )
        if publication_reservation is None and schedule_reservation is None:
            return CanonicalRepeatReservationVerification(safe_source_id, "ineligible")
        if (
            publication_reservation is None
            or schedule_reservation is None
            or publication_reservation != schedule_reservation
        ):
            return CanonicalRepeatReservationVerification(safe_source_id, "conflict")
        reservation = publication_reservation

        if reservation.get("version") != 1:
            return CanonicalRepeatReservationVerification(safe_source_id, "conflict")
        reserved_source_id = _positive_int(reservation.get("source_publication_id"))
        reserved_schedule_id = _positive_int(reservation.get("source_schedule_entry_id"))
        repeat_group_id = _positive_int(reservation.get("repeat_group_id"))
        channel_id = _positive_int(reservation.get("channel_id"))
        content_item_id = _positive_int(reservation.get("content_item_id"))
        content_revision = _positive_int(reservation.get("content_revision"))
        repeat_seconds = _positive_int(reservation.get("repeat_seconds"))
        expected_at = _scheduled_at(reservation.get("scheduled_at"))
        reserved_options = _mapping(reservation.get("runtime_options"))
        if None in (
            reserved_source_id,
            reserved_schedule_id,
            repeat_group_id,
            channel_id,
            content_item_id,
            content_revision,
            repeat_seconds,
            expected_at,
            reserved_options,
        ):
            return CanonicalRepeatReservationVerification(safe_source_id, "conflict")
        assert repeat_group_id is not None
        assert channel_id is not None
        assert content_item_id is not None
        assert content_revision is not None
        assert repeat_seconds is not None
        assert expected_at is not None
        assert reserved_options is not None

        repeat_rule = _mapping(schedule.repeat_rule)
        source_publication_options = _runtime_options(publication_meta)
        source_schedule_options = _runtime_options(schedule_meta)
        if (
            reserved_source_id != int(publication.id)
            or reserved_schedule_id != int(schedule.id)
            or channel_id != int(publication.channel_id)
            or content_item_id != int(publication.content_item_id)
            or content_revision != int(publication.content_revision)
            or _positive_int(publication_meta.get("repeat_group_id")) != repeat_group_id
            or _positive_int(schedule_meta.get("repeat_group_id")) != repeat_group_id
            or repeat_rule is None
            or repeat_rule.get("enabled") is not True
            or _positive_int(repeat_rule.get("seconds")) != repeat_seconds
            or source_publication_options != reserved_options
            or source_schedule_options != reserved_options
        ):
            return CanonicalRepeatReservationVerification(safe_source_id, "conflict")

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
                .where(
                    Publication.id != safe_source_id,
                    Publication.channel_id == channel_id,
                    Publication.content_item_id == content_item_id,
                    Publication.content_revision == content_revision,
                    ScheduleEntry.scheduled_at == expected_at,
                    ScheduleEntry.meta["repeat_group_id"].as_integer()
                    == repeat_group_id,
                )
                .order_by(Publication.id.asc())
                .limit(2)
            )
        ).all()
        if not rows:
            return CanonicalRepeatReservationVerification(safe_source_id, "pending")
        if len(rows) != 1:
            return CanonicalRepeatReservationVerification(safe_source_id, "conflict")

        successor, successor_schedule = rows[0]
        successor_meta = _mapping(successor.meta)
        successor_schedule_meta = _mapping(successor_schedule.meta)
        successor_rule = _mapping(successor_schedule.repeat_rule)
        legacy_task_id = _positive_int(successor.legacy_post_task_id)
        if (
            successor_meta is None
            or successor_schedule_meta is None
            or successor_rule is None
            or legacy_task_id is None
            or _positive_int(successor_meta.get("repeat_group_id")) != repeat_group_id
            or _positive_int(successor_schedule_meta.get("repeat_group_id"))
            != repeat_group_id
            or successor_rule.get("enabled") is not True
            or _positive_int(successor_rule.get("seconds")) != repeat_seconds
            or _runtime_options(successor_meta) != reserved_options
            or _runtime_options(successor_schedule_meta) != reserved_options
            or as_utc(successor_schedule.scheduled_at) != expected_at
        ):
            return CanonicalRepeatReservationVerification(safe_source_id, "conflict")

        return CanonicalRepeatReservationVerification(
            source_publication_id=safe_source_id,
            outcome="matched",
            successor_publication_id=int(successor.id),
            successor_schedule_entry_id=int(successor_schedule.id),
            successor_legacy_post_task_id=legacy_task_id,
        )
