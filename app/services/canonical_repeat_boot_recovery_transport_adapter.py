from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_repeat_boot_recovery_planner import MAX_BOOT_GROUP_SOURCES
from app.services.canonical_repeat_boot_recovery_reservation import (
    CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY,
)
from app.services.canonical_repeat_boot_recovery_verifier import (
    CanonicalRepeatBootRecoveryVerifier,
)
from app.services.publication_bridge import (
    PublicationBridgeError,
    _delivery_meta,
    _runtime_intent,
    _scheduler_payload,
)
from app.services.rich_media_assets import RichMediaAssetError, RichMediaAssetResolver
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatBootRecoveryTransportResult:
    source_publication_ids: tuple[int, ...]
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


def _dedupe_key(repeat_group_id: int, scheduled_at: datetime) -> str:
    return (
        f"canonical-repeat-boot-recovery:{int(repeat_group_id)}:"
        f"{as_utc(scheduled_at).isoformat()}"
    )


class CanonicalRepeatBootRecoveryTransportAdapter:
    """Atomically fulfill one reserved boot repeat group through legacy transport.

    The canonical group reservation is the planning authority. Exactly one future
    ScheduleEntry/Publication/PostTask adapter is materialized while every reserved
    overdue source receives canonical skipped audit in the same transaction. This
    service is intentionally not wired into ``_boot_cleanup_repeats`` yet.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_sources(
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
                .with_for_update()
            )
        ).all()
        if len(rows) != len(source_ids):
            return None
        locked = {
            int(publication.id): (publication, schedule)
            for publication, schedule in rows
        }
        return locked if len(locked) == len(source_ids) else None

    async def _exact_target_transport_ids(
        self,
        *,
        channel_id: int,
        repeat_group_id: int,
        scheduled_at: datetime,
    ) -> tuple[int, ...]:
        rows = (
            await self.session.execute(
                select(PostTask)
                .where(
                    PostTask.channel_id == int(channel_id),
                    PostTask.payload["repeat_group_id"].as_integer()
                    == int(repeat_group_id),
                )
                .order_by(PostTask.id.asc())
                .limit(200)
            )
        ).scalars().all()
        expected = as_utc(scheduled_at)
        return tuple(
            int(task.id)
            for task in rows
            if task.scheduled_at is not None and as_utc(task.scheduled_at) == expected
        )

    async def _source_transport_is_exact(
        self,
        publication: Publication,
        *,
        source_scheduled_at: datetime,
        repeat_group_id: int,
        repeat_seconds: int,
        runtime_options: Mapping[str, Any],
    ) -> tuple[bool, PostTask | None]:
        if publication.legacy_post_task_id is None:
            return True, None
        task = await self.session.get(PostTask, int(publication.legacy_post_task_id))
        if task is None:
            return False, None
        if (
            str(task.status) != "pending"
            or int(task.channel_id) != int(publication.channel_id)
            or task.scheduled_at is None
            or as_utc(task.scheduled_at) != as_utc(source_scheduled_at)
        ):
            return False, task

        payload = _mapping(task.payload)
        if payload is None or payload.get("repeat_on") is not True:
            return False, task
        if _positive_int(payload.get("repeat_seconds")) != int(repeat_seconds):
            return False, task
        payload_group = _positive_int(payload.get("repeat_group_id"))
        if payload_group is not None:
            if payload_group != int(repeat_group_id):
                return False, task
        elif int(task.id) != int(repeat_group_id):
            return False, task
        for key, value in runtime_options.items():
            if key not in payload or payload.get(key) != value:
                return False, task
        return True, task

    async def _existing_after_integrity_conflict(
        self,
        source_ids: tuple[int, ...],
    ) -> CanonicalRepeatBootRecoveryTransportResult:
        verification = await CanonicalRepeatBootRecoveryVerifier(self.session).verify(
            source_ids
        )
        if verification.outcome == "matched":
            return CanonicalRepeatBootRecoveryTransportResult(
                source_publication_ids=source_ids,
                outcome="existing",
                publication_id=verification.successor_publication_id,
                schedule_entry_id=verification.successor_schedule_entry_id,
                legacy_post_task_id=verification.successor_legacy_post_task_id,
            )
        return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")

    async def materialize(
        self,
        source_publication_ids: Sequence[int],
    ) -> CanonicalRepeatBootRecoveryTransportResult:
        source_ids = _source_ids(source_publication_ids)
        if source_ids is None:
            return CanonicalRepeatBootRecoveryTransportResult((), "ineligible")

        locked = await self._lock_sources(source_ids)
        if locked is None:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(source_ids, "ineligible")

        verification = await CanonicalRepeatBootRecoveryVerifier(self.session).verify(
            source_ids
        )
        if verification.outcome == "matched":
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(
                source_publication_ids=source_ids,
                outcome="existing",
                publication_id=verification.successor_publication_id,
                schedule_entry_id=verification.successor_schedule_entry_id,
                legacy_post_task_id=verification.successor_legacy_post_task_id,
            )
        if verification.outcome != "pending":
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(
                source_publication_ids=source_ids,
                outcome=(
                    "ineligible" if verification.outcome == "ineligible" else "conflict"
                ),
            )

        reservation: dict[str, Any] | None = None
        for publication_id in source_ids:
            publication, schedule = locked[publication_id]
            publication_meta = _mapping(publication.meta)
            schedule_meta = _mapping(schedule.meta)
            if publication_meta is None or schedule_meta is None:
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
            publication_reservation = _mapping(
                publication_meta.get(CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY)
            )
            schedule_reservation = _mapping(
                schedule_meta.get(CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY)
            )
            if (
                publication_reservation is None
                or schedule_reservation is None
                or publication_reservation != schedule_reservation
            ):
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
            if reservation is None:
                reservation = publication_reservation
            elif reservation != publication_reservation:
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")

        if reservation is None or reservation.get("version") != 1:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")

        raw_sources = reservation.get("sources")
        if not isinstance(raw_sources, list) or len(raw_sources) != len(source_ids):
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
        reserved_sources: list[tuple[int, int, datetime]] = []
        for raw_source in raw_sources:
            source = _mapping(raw_source)
            if source is None:
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
            publication_id = _positive_int(source.get("publication_id"))
            schedule_id = _positive_int(source.get("schedule_entry_id"))
            scheduled_at = _scheduled_at(source.get("scheduled_at"))
            if publication_id is None or schedule_id is None or scheduled_at is None:
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
            reserved_sources.append((publication_id, schedule_id, scheduled_at))
        if tuple(source[0] for source in reserved_sources) != source_ids:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")

        anchor_publication_id = _positive_int(reservation.get("anchor_publication_id"))
        anchor_schedule_id = _positive_int(reservation.get("anchor_schedule_entry_id"))
        repeat_group_id = _positive_int(reservation.get("repeat_group_id"))
        channel_id = _positive_int(reservation.get("channel_id"))
        content_item_id = _positive_int(reservation.get("content_item_id"))
        content_revision = _positive_int(reservation.get("content_revision"))
        repeat_seconds = _positive_int(reservation.get("repeat_seconds"))
        expected_at = _scheduled_at(reservation.get("scheduled_at"))
        runtime_options = _mapping(reservation.get("runtime_options"))
        if None in (
            anchor_publication_id,
            anchor_schedule_id,
            repeat_group_id,
            channel_id,
            content_item_id,
            content_revision,
            repeat_seconds,
            expected_at,
            runtime_options,
        ):
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
        assert anchor_publication_id is not None
        assert anchor_schedule_id is not None
        assert repeat_group_id is not None
        assert channel_id is not None
        assert content_item_id is not None
        assert content_revision is not None
        assert repeat_seconds is not None
        assert expected_at is not None
        assert runtime_options is not None
        if (
            anchor_publication_id != reserved_sources[0][0]
            or anchor_schedule_id != reserved_sources[0][1]
        ):
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")

        target_transport_ids = await self._exact_target_transport_ids(
            channel_id=channel_id,
            repeat_group_id=repeat_group_id,
            scheduled_at=expected_at,
        )
        if target_transport_ids:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(
                source_publication_ids=source_ids,
                outcome=(
                    "existing_transport"
                    if len(target_transport_ids) == 1
                    else "conflict"
                ),
                legacy_post_task_id=(
                    target_transport_ids[0] if len(target_transport_ids) == 1 else None
                ),
            )

        source_tasks: dict[int, PostTask] = {}
        timezone_name: str | None = None
        for publication_id, schedule_id, source_at in reserved_sources:
            publication, schedule = locked[publication_id]
            if (
                int(schedule.id) != schedule_id
                or as_utc(schedule.scheduled_at) != source_at
                or int(publication.channel_id) != channel_id
                or int(publication.content_item_id) != content_item_id
                or int(publication.content_revision) != content_revision
            ):
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
            if publication_id == anchor_publication_id:
                timezone_name = schedule.timezone
            exact, task = await self._source_transport_is_exact(
                publication,
                source_scheduled_at=source_at,
                repeat_group_id=repeat_group_id,
                repeat_seconds=repeat_seconds,
                runtime_options=runtime_options,
            )
            if not exact:
                await self.session.rollback()
                return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
            if task is not None:
                task_id = int(task.id)
                if task_id in source_tasks:
                    await self.session.rollback()
                    return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")
                source_tasks[task_id] = task

        item = await self.session.get(ContentItem, content_item_id)
        revision = (
            await self.session.execute(
                select(ContentRevision).where(
                    ContentRevision.content_item_id == content_item_id,
                    ContentRevision.revision == content_revision,
                )
            )
        ).scalar_one_or_none()
        if item is None or revision is None or int(item.channel_id) != channel_id:
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")

        try:
            document = PostDocument.from_dict(revision.document)
            render_document = await RichMediaAssetResolver(self.session).resolve(
                document,
                channel_id=channel_id,
            )
            payload = _scheduler_payload(document, render_document=render_document)
            payload["repeat_on"] = True
            payload["repeat_seconds"] = repeat_seconds
            payload["repeat_group_id"] = repeat_group_id
            runtime_intent = _runtime_intent(runtime_options, payload=payload)
            for key, value in runtime_intent.items():
                payload[key] = deepcopy(value)
        except (PublicationBridgeError, RichMediaAssetError, ValueError, TypeError):
            await self.session.rollback()
            return CanonicalRepeatBootRecoveryTransportResult(source_ids, "conflict")

        autodelete_seconds = _positive_int(runtime_intent.get("autodelete_seconds"))
        if autodelete_seconds is not None:
            payload["autodelete_at"] = (
                expected_at + timedelta(seconds=autodelete_seconds)
            ).isoformat()

        canonical_meta = _delivery_meta(None, runtime_intent)
        canonical_meta = {
            **canonical_meta,
            "repeat_group_id": repeat_group_id,
            "canonical_repeat_boot_recovery_anchor_publication_id": (
                anchor_publication_id
            ),
            "canonical_repeat_boot_recovery_source_publication_ids": list(source_ids),
            "canonical_repeat_boot_recovery_transport_adapter": True,
            "reused_content_provenance": True,
            "repeat_root_provenance": True,
        }
        child_schedule = ScheduleEntry(
            content_item_id=content_item_id,
            content_revision=content_revision,
            channel_id=channel_id,
            scheduled_at=expected_at,
            timezone=timezone_name,
            status="pending",
            repeat_rule={"enabled": True, "seconds": repeat_seconds},
            meta=deepcopy(canonical_meta),
        )
        child_publication = Publication(
            schedule_entry_id=None,
            content_item_id=content_item_id,
            content_revision=content_revision,
            channel_id=channel_id,
            status="queued",
            meta=deepcopy(canonical_meta),
        )
        self.session.add_all([child_schedule, child_publication])

        try:
            await self.session.flush()
            child_publication.schedule_entry_id = int(child_schedule.id)
            child_task = PostTask(
                channel_id=channel_id,
                status="pending",
                payload=payload,
                dedupe_key=_dedupe_key(repeat_group_id, expected_at),
                scheduled_at=expected_at,
            )
            self.session.add(child_task)
            await self.session.flush()
            child_task_id = int(child_task.id)
            child_publication.legacy_post_task_id = child_task_id
            child_schedule.meta = {
                **dict(child_schedule.meta or {}),
                "legacy_post_task_id": child_task_id,
            }

            for publication_id in source_ids:
                publication, schedule = locked[publication_id]
                publication.status = "skipped"
                schedule.status = "skipped"
                publication.attempt_count = 1
                linked_task_id = (
                    int(publication.legacy_post_task_id)
                    if publication.legacy_post_task_id is not None
                    else None
                )
                self.session.add(
                    PublicationAttempt(
                        publication_id=publication_id,
                        attempt=1,
                        status="skipped",
                        telegram_message_ids=None,
                        error=None,
                        meta={
                            "canonical_repeat_boot_recovery": True,
                            **(
                                {"legacy_post_task_id": linked_task_id}
                                if linked_task_id is not None
                                else {}
                            ),
                        },
                        finished_at=datetime.now(timezone.utc),
                    )
                )

            for task in source_tasks.values():
                task.status = "skipped"
                task.error = "overdue at boot"

            await self.session.commit()
            return CanonicalRepeatBootRecoveryTransportResult(
                source_publication_ids=source_ids,
                outcome="created",
                publication_id=int(child_publication.id),
                schedule_entry_id=int(child_schedule.id),
                legacy_post_task_id=child_task_id,
            )
        except IntegrityError:
            await self.session.rollback()
            return await self._existing_after_integrity_conflict(source_ids)
        except Exception:
            await self.session.rollback()
            raise
