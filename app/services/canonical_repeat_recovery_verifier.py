from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_repeat_recovery_reservation import (
    CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY,
)
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatRecoveryVerification:
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


class CanonicalRepeatRecoveryVerifier:
    """Read-only proof that an overdue recovery reservation was fulfilled exactly."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _attempts(self, publication_id: int) -> list[PublicationAttempt]:
        return list(
            (
                await self.session.execute(
                    select(PublicationAttempt)
                    .where(PublicationAttempt.publication_id == int(publication_id))
                    .order_by(PublicationAttempt.attempt.asc())
                )
            ).scalars().all()
        )

    async def verify(
        self,
        source_publication_id: int,
    ) -> CanonicalRepeatRecoveryVerification:
        try:
            safe_source_id = int(source_publication_id)
        except (TypeError, ValueError, OverflowError):
            return CanonicalRepeatRecoveryVerification(0, "ineligible")
        if safe_source_id <= 0:
            return CanonicalRepeatRecoveryVerification(safe_source_id, "ineligible")

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
                .where(Publication.id == safe_source_id)
            )
        ).one_or_none()
        if source is None:
            return CanonicalRepeatRecoveryVerification(safe_source_id, "ineligible")
        publication, schedule = source

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if publication_meta is None or schedule_meta is None:
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")
        publication_reservation = _mapping(
            publication_meta.get(CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY)
        )
        schedule_reservation = _mapping(
            schedule_meta.get(CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY)
        )
        if publication_reservation is None and schedule_reservation is None:
            return CanonicalRepeatRecoveryVerification(safe_source_id, "ineligible")
        if (
            publication_reservation is None
            or schedule_reservation is None
            or publication_reservation != schedule_reservation
        ):
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")
        reservation = publication_reservation

        if reservation.get("version") != 1:
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")
        reserved_source_id = _positive_int(reservation.get("source_publication_id"))
        reserved_schedule_id = _positive_int(reservation.get("source_schedule_entry_id"))
        source_scheduled_at = _scheduled_at(reservation.get("source_scheduled_at"))
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
            source_scheduled_at,
            repeat_group_id,
            channel_id,
            content_item_id,
            content_revision,
            repeat_seconds,
            expected_at,
            reserved_options,
        ):
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")
        assert source_scheduled_at is not None
        assert repeat_group_id is not None
        assert channel_id is not None
        assert content_item_id is not None
        assert content_revision is not None
        assert repeat_seconds is not None
        assert expected_at is not None
        assert reserved_options is not None

        repeat_rule = _mapping(schedule.repeat_rule)
        if (
            reserved_source_id != int(publication.id)
            or reserved_schedule_id != int(schedule.id)
            or as_utc(schedule.scheduled_at) != source_scheduled_at
            or channel_id != int(publication.channel_id)
            or content_item_id != int(publication.content_item_id)
            or content_revision != int(publication.content_revision)
            or _positive_int(publication_meta.get("repeat_group_id")) != repeat_group_id
            or _positive_int(schedule_meta.get("repeat_group_id")) != repeat_group_id
            or repeat_rule is None
            or repeat_rule.get("enabled") is not True
            or _positive_int(repeat_rule.get("seconds")) != repeat_seconds
            or _runtime_options(publication_meta) != reserved_options
            or _runtime_options(schedule_meta) != reserved_options
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
        ):
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")

        attempts = await self._attempts(safe_source_id)
        pending_source = (
            str(publication.status) == "queued"
            and str(schedule.status) == "pending"
            and int(publication.attempt_count or 0) == 0
            and not attempts
        )
        skipped_source = (
            str(publication.status) == "skipped"
            and str(schedule.status) == "skipped"
            and int(publication.attempt_count or 0) == 1
            and len(attempts) == 1
            and int(attempts[0].attempt) == 1
            and str(attempts[0].status) == "skipped"
            and attempts[0].finished_at is not None
            and attempts[0].telegram_message_ids in (None, [])
            and attempts[0].error is None
        )
        if not pending_source and not skipped_source:
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")

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
            return CanonicalRepeatRecoveryVerification(
                safe_source_id,
                "pending" if pending_source else "conflict",
            )
        if len(rows) != 1:
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")

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
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")

        if not skipped_source:
            # A child cannot fulfill recovery until the overdue source is durably skipped.
            return CanonicalRepeatRecoveryVerification(safe_source_id, "conflict")

        return CanonicalRepeatRecoveryVerification(
            source_publication_id=safe_source_id,
            outcome="matched",
            successor_publication_id=int(successor.id),
            successor_schedule_entry_id=int(successor_schedule.id),
            successor_legacy_post_task_id=legacy_task_id,
        )
