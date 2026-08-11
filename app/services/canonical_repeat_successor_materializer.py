from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_plan_reservation import (
    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY,
)
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatSuccessorMaterialization:
    source_publication_id: int
    outcome: Literal[
        "created",
        "existing",
        "existing_transport",
        "ineligible",
        "conflict",
    ]
    publication_id: int | None = None
    schedule_entry_id: int | None = None
    legacy_post_task_id: int | None = None


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
        return as_utc(datetime.fromisoformat(text))
    except (TypeError, ValueError, OverflowError):
        return None


def _runtime_options(meta: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = meta.get("runtime_options")
    if raw is None:
        return {}
    return _mapping(raw)


def _successor_meta(
    *,
    source_publication_id: int,
    repeat_group_id: int,
    runtime_options: Mapping[str, Any],
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "repeat_group_id": int(repeat_group_id),
        "canonical_repeat_source_publication_id": int(source_publication_id),
        "canonical_repeat_successor_materializer": True,
        "reused_content_provenance": True,
        "repeat_root_provenance": True,
    }
    if runtime_options:
        meta["runtime_options"] = deepcopy(dict(runtime_options))
    return meta


class CanonicalRepeatSuccessorMaterializer:
    """Create one reserved repeat successor without a compatibility ``PostTask``.

    The source reservation remains the planning authority. Locking the terminal source
    serializes materialization attempts for that source; an exact already-existing
    canonical successor is returned idempotently instead of duplicated. A successor
    that already has legacy transport is reported explicitly so callers can avoid
    starting a second delivery authority during migration.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_source(
        self,
        source_publication_id: int,
    ) -> tuple[Publication, ScheduleEntry] | None:
        return (
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
                    Publication.id == int(source_publication_id),
                    Publication.status == "published",
                    ScheduleEntry.status == "completed",
                )
                .with_for_update()
            )
        ).one_or_none()

    async def _existing(
        self,
        *,
        source_publication_id: int,
        repeat_group_id: int,
        channel_id: int,
        content_item_id: int,
        content_revision: int,
        repeat_seconds: int,
        scheduled_at: datetime,
        runtime_options: Mapping[str, Any],
    ) -> CanonicalRepeatSuccessorMaterialization | None:
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
                    Publication.id != int(source_publication_id),
                    Publication.channel_id == int(channel_id),
                    Publication.content_item_id == int(content_item_id),
                    Publication.content_revision == int(content_revision),
                    ScheduleEntry.scheduled_at == as_utc(scheduled_at),
                    ScheduleEntry.meta["repeat_group_id"].as_integer()
                    == int(repeat_group_id),
                )
                .order_by(Publication.id.asc())
                .limit(2)
            )
        ).all()
        if not rows:
            return None
        if len(rows) != 1:
            return CanonicalRepeatSuccessorMaterialization(
                source_publication_id=int(source_publication_id),
                outcome="conflict",
            )

        publication, schedule = rows[0]
        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        repeat_rule = _mapping(schedule.repeat_rule)
        if (
            publication_meta is None
            or schedule_meta is None
            or repeat_rule is None
            or _positive_int(publication_meta.get("repeat_group_id"))
            != int(repeat_group_id)
            or _positive_int(schedule_meta.get("repeat_group_id"))
            != int(repeat_group_id)
            or _positive_int(publication_meta.get("canonical_repeat_source_publication_id"))
            != int(source_publication_id)
            or _positive_int(schedule_meta.get("canonical_repeat_source_publication_id"))
            != int(source_publication_id)
            or repeat_rule.get("enabled") is not True
            or _positive_int(repeat_rule.get("seconds")) != int(repeat_seconds)
            or _runtime_options(publication_meta) != dict(runtime_options)
            or _runtime_options(schedule_meta) != dict(runtime_options)
            or as_utc(schedule.scheduled_at) != as_utc(scheduled_at)
            or publication.status != "queued"
            or schedule.status != "pending"
        ):
            return CanonicalRepeatSuccessorMaterialization(
                source_publication_id=int(source_publication_id),
                outcome="conflict",
            )

        legacy_task_id = _positive_int(publication.legacy_post_task_id)
        if legacy_task_id is not None:
            return CanonicalRepeatSuccessorMaterialization(
                source_publication_id=int(source_publication_id),
                outcome="existing_transport",
                publication_id=int(publication.id),
                schedule_entry_id=int(schedule.id),
                legacy_post_task_id=legacy_task_id,
            )
        if (
            publication_meta.get("canonical_repeat_successor_materializer") is not True
            or schedule_meta.get("canonical_repeat_successor_materializer") is not True
        ):
            return CanonicalRepeatSuccessorMaterialization(
                source_publication_id=int(source_publication_id),
                outcome="conflict",
            )
        return CanonicalRepeatSuccessorMaterialization(
            source_publication_id=int(source_publication_id),
            outcome="existing",
            publication_id=int(publication.id),
            schedule_entry_id=int(schedule.id),
            legacy_post_task_id=None,
        )

    async def materialize(
        self,
        source_publication_id: int,
    ) -> CanonicalRepeatSuccessorMaterialization:
        try:
            safe_source_id = int(source_publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_source_id = 0
        if safe_source_id <= 0:
            return CanonicalRepeatSuccessorMaterialization(
                source_publication_id=safe_source_id,
                outcome="ineligible",
            )

        source = await self._lock_source(safe_source_id)
        if source is None:
            await self.session.rollback()
            return CanonicalRepeatSuccessorMaterialization(
                source_publication_id=safe_source_id,
                outcome="ineligible",
            )
        publication, schedule = source

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if publication_meta is None or schedule_meta is None:
            await self.session.rollback()
            return CanonicalRepeatSuccessorMaterialization(safe_source_id, "conflict")
        publication_reservation = _mapping(
            publication_meta.get(CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY)
        )
        schedule_reservation = _mapping(
            schedule_meta.get(CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY)
        )
        if (
            publication_reservation is None
            or schedule_reservation is None
            or publication_reservation != schedule_reservation
        ):
            await self.session.rollback()
            return CanonicalRepeatSuccessorMaterialization(safe_source_id, "ineligible")
        reservation = publication_reservation

        if reservation.get("version") != 1:
            await self.session.rollback()
            return CanonicalRepeatSuccessorMaterialization(safe_source_id, "conflict")
        reserved_source_id = _positive_int(reservation.get("source_publication_id"))
        reserved_schedule_id = _positive_int(reservation.get("source_schedule_entry_id"))
        repeat_group_id = _positive_int(reservation.get("repeat_group_id"))
        channel_id = _positive_int(reservation.get("channel_id"))
        content_item_id = _positive_int(reservation.get("content_item_id"))
        content_revision = _positive_int(reservation.get("content_revision"))
        repeat_seconds = _positive_int(reservation.get("repeat_seconds"))
        expected_at = _scheduled_at(reservation.get("scheduled_at"))
        runtime_options = _mapping(reservation.get("runtime_options"))
        if None in (
            reserved_source_id,
            reserved_schedule_id,
            repeat_group_id,
            channel_id,
            content_item_id,
            content_revision,
            repeat_seconds,
            expected_at,
            runtime_options,
        ):
            await self.session.rollback()
            return CanonicalRepeatSuccessorMaterialization(safe_source_id, "conflict")
        assert reserved_source_id is not None
        assert reserved_schedule_id is not None
        assert repeat_group_id is not None
        assert channel_id is not None
        assert content_item_id is not None
        assert content_revision is not None
        assert repeat_seconds is not None
        assert expected_at is not None
        assert runtime_options is not None

        repeat_rule = _mapping(schedule.repeat_rule)
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
            or _runtime_options(publication_meta) != runtime_options
            or _runtime_options(schedule_meta) != runtime_options
        ):
            await self.session.rollback()
            return CanonicalRepeatSuccessorMaterialization(safe_source_id, "conflict")

        existing = await self._existing(
            source_publication_id=safe_source_id,
            repeat_group_id=repeat_group_id,
            channel_id=channel_id,
            content_item_id=content_item_id,
            content_revision=content_revision,
            repeat_seconds=repeat_seconds,
            scheduled_at=expected_at,
            runtime_options=runtime_options,
        )
        if existing is not None:
            await self.session.rollback()
            return existing

        item = await self.session.get(ContentItem, content_item_id)
        revision = (
            await self.session.execute(
                select(ContentRevision).where(
                    ContentRevision.content_item_id == content_item_id,
                    ContentRevision.revision == content_revision,
                )
            )
        ).scalar_one_or_none()
        if (
            item is None
            or revision is None
            or int(item.channel_id) != channel_id
        ):
            await self.session.rollback()
            return CanonicalRepeatSuccessorMaterialization(safe_source_id, "conflict")

        meta = _successor_meta(
            source_publication_id=safe_source_id,
            repeat_group_id=repeat_group_id,
            runtime_options=runtime_options,
        )
        successor_schedule = ScheduleEntry(
            content_item_id=content_item_id,
            content_revision=content_revision,
            channel_id=channel_id,
            scheduled_at=expected_at,
            timezone=schedule.timezone,
            status="pending",
            repeat_rule={"enabled": True, "seconds": repeat_seconds},
            meta=deepcopy(meta),
        )
        successor = Publication(
            schedule_entry_id=None,
            content_item_id=content_item_id,
            content_revision=content_revision,
            channel_id=channel_id,
            status="queued",
            legacy_post_task_id=None,
            meta=deepcopy(meta),
        )
        self.session.add_all([successor_schedule, successor])
        try:
            await self.session.flush()
            successor.schedule_entry_id = int(successor_schedule.id)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        return CanonicalRepeatSuccessorMaterialization(
            source_publication_id=safe_source_id,
            outcome="created",
            publication_id=int(successor.id),
            schedule_entry_id=int(successor_schedule.id),
            legacy_post_task_id=None,
        )
