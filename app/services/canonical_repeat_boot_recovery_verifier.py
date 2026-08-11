from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_repeat_boot_recovery_planner import MAX_BOOT_GROUP_SOURCES
from app.services.canonical_repeat_boot_recovery_reservation import (
    CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY,
)
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatBootRecoveryVerification:
    source_publication_ids: tuple[int, ...]
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


class CanonicalRepeatBootRecoveryVerifier:
    """Read-only proof that one reserved boot repeat group was fulfilled exactly."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _sources(
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
            )
        ).all()
        if len(rows) != len(source_ids):
            return None
        result = {int(publication.id): (publication, schedule) for publication, schedule in rows}
        return result if len(result) == len(source_ids) else None

    async def _attempts(
        self,
        source_ids: tuple[int, ...],
    ) -> dict[int, list[PublicationAttempt]]:
        grouped = {publication_id: [] for publication_id in source_ids}
        attempts = (
            await self.session.execute(
                select(PublicationAttempt)
                .where(PublicationAttempt.publication_id.in_(source_ids))
                .order_by(
                    PublicationAttempt.publication_id.asc(),
                    PublicationAttempt.attempt.asc(),
                )
            )
        ).scalars().all()
        for attempt in attempts:
            grouped.setdefault(int(attempt.publication_id), []).append(attempt)
        return grouped

    async def verify(
        self,
        source_publication_ids: Sequence[int],
    ) -> CanonicalRepeatBootRecoveryVerification:
        source_ids = _source_ids(source_publication_ids)
        if source_ids is None:
            return CanonicalRepeatBootRecoveryVerification((), "ineligible")

        sources = await self._sources(source_ids)
        if sources is None:
            return CanonicalRepeatBootRecoveryVerification(source_ids, "ineligible")

        reservation_values: list[Any] = []
        source_meta: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        for publication_id in source_ids:
            publication, schedule = sources[publication_id]
            publication_meta = _mapping(publication.meta)
            schedule_meta = _mapping(schedule.meta)
            if publication_meta is None or schedule_meta is None:
                return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
            source_meta[publication_id] = (publication_meta, schedule_meta)
            reservation_values.extend(
                [
                    publication_meta.get(
                        CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY
                    ),
                    schedule_meta.get(CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY),
                ]
            )

        if all(value is None for value in reservation_values):
            return CanonicalRepeatBootRecoveryVerification(source_ids, "ineligible")
        reservations = [_mapping(value) for value in reservation_values]
        if any(value is None for value in reservations):
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
        reservation = reservations[0]
        assert reservation is not None
        if any(value != reservation for value in reservations[1:]):
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
        if reservation.get("version") != 1:
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")

        raw_sources = reservation.get("sources")
        if not isinstance(raw_sources, list) or len(raw_sources) != len(source_ids):
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
        reserved_sources: list[tuple[int, int, datetime]] = []
        previous_at: datetime | None = None
        for raw_source in raw_sources:
            source = _mapping(raw_source)
            if source is None:
                return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
            publication_id = _positive_int(source.get("publication_id"))
            schedule_id = _positive_int(source.get("schedule_entry_id"))
            scheduled_at = _scheduled_at(source.get("scheduled_at"))
            if publication_id is None or schedule_id is None or scheduled_at is None:
                return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
            if previous_at is not None and scheduled_at < previous_at:
                return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
            previous_at = scheduled_at
            reserved_sources.append((publication_id, schedule_id, scheduled_at))

        if tuple(source[0] for source in reserved_sources) != source_ids:
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
        if len({source[0] for source in reserved_sources}) != len(source_ids):
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")

        anchor_publication_id = _positive_int(reservation.get("anchor_publication_id"))
        anchor_schedule_id = _positive_int(reservation.get("anchor_schedule_entry_id"))
        repeat_group_id = _positive_int(reservation.get("repeat_group_id"))
        channel_id = _positive_int(reservation.get("channel_id"))
        content_item_id = _positive_int(reservation.get("content_item_id"))
        content_revision = _positive_int(reservation.get("content_revision"))
        repeat_seconds = _positive_int(reservation.get("repeat_seconds"))
        expected_at = _scheduled_at(reservation.get("scheduled_at"))
        reserved_options = _mapping(reservation.get("runtime_options"))
        if None in (
            anchor_publication_id,
            anchor_schedule_id,
            repeat_group_id,
            channel_id,
            content_item_id,
            content_revision,
            repeat_seconds,
            expected_at,
            reserved_options,
        ):
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
        assert anchor_publication_id is not None
        assert anchor_schedule_id is not None
        assert repeat_group_id is not None
        assert channel_id is not None
        assert content_item_id is not None
        assert content_revision is not None
        assert repeat_seconds is not None
        assert expected_at is not None
        assert reserved_options is not None
        if (
            anchor_publication_id != reserved_sources[0][0]
            or anchor_schedule_id != reserved_sources[0][1]
        ):
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")

        attempts = await self._attempts(source_ids)
        states: list[str] = []
        for publication_id, schedule_id, source_at in reserved_sources:
            publication, schedule = sources[publication_id]
            publication_meta, schedule_meta = source_meta[publication_id]
            repeat_rule = _mapping(schedule.repeat_rule)
            if (
                int(schedule.id) != schedule_id
                or as_utc(schedule.scheduled_at) != source_at
                or int(publication.channel_id) != channel_id
                or int(publication.content_item_id) != content_item_id
                or int(publication.content_revision) != content_revision
                or _positive_int(publication_meta.get("repeat_group_id"))
                != repeat_group_id
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
                return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")

            publication_attempts = attempts.get(publication_id, [])
            pending = (
                str(publication.status) == "queued"
                and str(schedule.status) == "pending"
                and int(publication.attempt_count or 0) == 0
                and not publication_attempts
            )
            skipped = (
                str(publication.status) == "skipped"
                and str(schedule.status) == "skipped"
                and int(publication.attempt_count or 0) == 1
                and len(publication_attempts) == 1
                and int(publication_attempts[0].attempt) == 1
                and str(publication_attempts[0].status) == "skipped"
                and publication_attempts[0].finished_at is not None
                and publication_attempts[0].telegram_message_ids in (None, [])
                and publication_attempts[0].error is None
            )
            if not pending and not skipped:
                return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
            states.append("pending" if pending else "skipped")

        if len(set(states)) != 1:
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")
        source_state = states[0]

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
                    ~Publication.id.in_(source_ids),
                    Publication.channel_id == channel_id,
                    Publication.content_item_id == content_item_id,
                    Publication.content_revision == content_revision,
                    ScheduleEntry.scheduled_at == expected_at,
                    ScheduleEntry.meta["repeat_group_id"].as_integer() == repeat_group_id,
                )
                .order_by(Publication.id.asc())
                .limit(2)
            )
        ).all()
        if not rows:
            return CanonicalRepeatBootRecoveryVerification(
                source_ids,
                "pending" if source_state == "pending" else "conflict",
            )
        if len(rows) != 1:
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")

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
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")

        if source_state != "skipped":
            return CanonicalRepeatBootRecoveryVerification(source_ids, "conflict")

        return CanonicalRepeatBootRecoveryVerification(
            source_publication_ids=source_ids,
            outcome="matched",
            successor_publication_id=int(successor.id),
            successor_schedule_entry_id=int(successor_schedule.id),
            successor_legacy_post_task_id=legacy_task_id,
        )
