from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.content import LegacyPayloadError, document_from_legacy_payload
from app.services.publication_runtime import (
    AUTODELETE_RUNTIME_META_KEY,
    normalize_autodelete_runtime,
)
from app.services.scheduler_errors import public_scheduler_error
from app.services.scheduling import as_utc, cleanup_runtime_fields
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


_TERMINAL_STATUS = {
    "done": "published",
    "failed": "failed",
    "skipped": "skipped",
    "cancelled": "cancelled",
}

_RUNTIME_ONLY_FIELDS = frozenset(
    {
        "_publication_id",
        "_content_item_id",
        "_content_revision",
        "_content_channel_id",
        "repeat_on",
        "repeat_seconds",
        "repeat_group_id",
        "autodelete_at",
        "autodeleted",
        "autodeleted_at",
        "autodelete_effective_seconds",
    }
)
_MAX_DB_ID = (1 << 63) - 1


def _legacy_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _legacy_db_id(value: Any) -> int | None:
    parsed = _legacy_int(value)
    if parsed is None or parsed <= 0 or parsed > _MAX_DB_ID:
        return None
    return parsed


def _repeat_group_id(payload: dict[str, Any], *, task_id: int) -> int | None:
    if not bool(payload.get("repeat_on", False)):
        return None
    raw_group = payload.get("repeat_group_id")
    if raw_group is None:
        return int(task_id)
    return _legacy_db_id(raw_group)


async def _backfill_repeat_group_metadata(
    session: AsyncSession,
    *,
    task_id: int,
    channel_id: int,
    payload: dict[str, Any],
    publication: Publication,
) -> None:
    """Idempotently add a canonical repeat anchor to an already mirrored occurrence."""
    group_id = _repeat_group_id(payload, task_id=task_id)
    if group_id is None or int(publication.channel_id) != int(channel_id):
        return
    if publication.schedule_entry_id is None:
        return
    schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
    if schedule is None or int(schedule.channel_id) != int(channel_id):
        return
    if not isinstance(publication.meta, dict) or not isinstance(schedule.meta, dict):
        return

    publication_meta = dict(publication.meta)
    schedule_meta = dict(schedule.meta)
    publication_group = publication_meta.get("repeat_group_id")
    schedule_group = schedule_meta.get("repeat_group_id")
    for existing in (publication_group, schedule_group):
        if existing is None:
            continue
        if _legacy_db_id(existing) != group_id:
            # Never overwrite contradictory canonical provenance.
            return

    changed = False
    if publication_group is None:
        publication_meta["repeat_group_id"] = group_id
        publication.meta = publication_meta
        changed = True
    if schedule_group is None:
        schedule_meta["repeat_group_id"] = group_id
        schedule.meta = schedule_meta
        changed = True
    if changed:
        await session.commit()
        await session.refresh(publication)


def _content_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = cleanup_runtime_fields(payload)
    for key in _RUNTIME_ONLY_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def _repeat_rule(payload: dict[str, Any]) -> dict[str, Any]:
    if not bool(payload.get("repeat_on", False)):
        return {}
    seconds = _legacy_int(payload.get("repeat_seconds"))
    if seconds is None:
        return {"enabled": False, "seconds": 0}
    return {"enabled": seconds > 0, "seconds": max(0, seconds)}


def _author_id(payload: dict[str, Any]) -> int | None:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    return _legacy_db_id(meta.get("author_user_id"))


def _title_from_document_text(text: str) -> str | None:
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    return normalized[:120]


async def _content_identity(
    session: AsyncSession,
    *,
    item_id: int,
    revision_number: int,
    channel_id: int,
) -> tuple[ContentItem, ContentRevision] | None:
    item = await session.get(ContentItem, int(item_id))
    if item is None or int(item.channel_id) != int(channel_id):
        return None

    revision = (
        await session.execute(
            select(ContentRevision).where(
                ContentRevision.content_item_id == int(item_id),
                ContentRevision.revision == int(revision_number),
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        return None
    return item, revision


async def _referenced_content(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    channel_id: int,
) -> tuple[ContentItem, ContentRevision] | None:
    item_id = _legacy_db_id(payload.get("_content_item_id"))
    revision_number = _legacy_db_id(payload.get("_content_revision"))
    if item_id is None or revision_number is None:
        return None

    channel_marker = payload.get("_content_channel_id")
    if channel_marker is not None:
        marker_id = _legacy_db_id(channel_marker)
        if marker_id is None or marker_id != int(channel_id):
            return None

    return await _content_identity(
        session,
        item_id=item_id,
        revision_number=revision_number,
        channel_id=channel_id,
    )


async def _repeat_root_content(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    channel_id: int,
    task_id: int,
) -> tuple[ContentItem, ContentRevision] | None:
    """Resolve repeat provenance from linked or canonical repeat-group state."""
    root_task_id = _legacy_db_id(payload.get("repeat_group_id"))
    if root_task_id is None or root_task_id == int(task_id):
        return None

    root_publication = (
        await session.execute(
            select(Publication).where(
                Publication.legacy_post_task_id == root_task_id,
                Publication.channel_id == int(channel_id),
            )
        )
    ).scalar_one_or_none()
    if root_publication is None:
        root_publication = (
            await session.execute(
                select(Publication)
                .join(
                    ScheduleEntry,
                    ScheduleEntry.id == Publication.schedule_entry_id,
                )
                .where(
                    Publication.channel_id == int(channel_id),
                    ScheduleEntry.channel_id == int(channel_id),
                    ScheduleEntry.meta["repeat_group_id"].as_integer()
                    == root_task_id,
                )
                .order_by(Publication.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if root_publication is None:
        return None

    return await _content_identity(
        session,
        item_id=int(root_publication.content_item_id),
        revision_number=int(root_publication.content_revision),
        channel_id=channel_id,
    )


async def mirror_legacy_post_task(
    session: AsyncSession,
    task: PostTask,
) -> Publication | None:
    """Idempotently mirror one legacy PostTask into the new content domain.

    The legacy task remains the delivery source of truth during migration. A mirror
    failure must never alter task status or prevent the existing scheduler from
    processing it. Repeat tasks may reuse immutable ContentItem/ContentRevision
    provenance, but every delivery occurrence gets a distinct Publication.
    """
    task_id = int(task.id)
    channel_id = int(task.channel_id)
    payload = deepcopy(dict(task.payload or {}))
    existing = (
        await session.execute(
            select(Publication).where(Publication.legacy_post_task_id == task_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        await _backfill_repeat_group_metadata(
            session,
            task_id=task_id,
            channel_id=channel_id,
            payload=payload,
            publication=existing,
        )
        return existing

    publication_marker = _legacy_db_id(payload.get("_publication_id"))
    if publication_marker is not None:
        marked = await session.get(Publication, publication_marker)
        if marked is not None and int(marked.legacy_post_task_id or 0) == task_id:
            await _backfill_repeat_group_metadata(
                session,
                task_id=task_id,
                channel_id=channel_id,
                payload=payload,
                publication=marked,
            )
            return marked
    if "_publication_id" in payload:
        # Old repeat payloads could inherit the parent's publication marker. Treat
        # invalid/stale delivery identity as migration input and remove it while
        # retaining safe content provenance.
        payload.pop("_publication_id", None)

    referenced = await _referenced_content(
        session,
        payload,
        channel_id=channel_id,
    )
    reused_repeat_root = False
    if referenced is None:
        referenced = await _repeat_root_content(
            session,
            payload,
            channel_id=channel_id,
            task_id=task_id,
        )
        reused_repeat_root = referenced is not None

    document = None
    if referenced is None:
        try:
            document = document_from_legacy_payload(_content_payload(payload))
        except LegacyPayloadError as exc:
            logger.info(
                "Legacy content mirror skipped unsupported PostTask id={} type={} err={}",
                task_id,
                payload.get("type"),
                exc,
            )
            return None

    task_status = str(task.status or "pending")
    publication_status = {
        "pending": "queued",
        "processing": "sending",
        **_TERMINAL_STATUS,
    }.get(task_status, task_status)
    schedule_status = {
        "done": "completed",
        "failed": "failed",
        "skipped": "skipped",
        "cancelled": "cancelled",
    }.get(task_status, "pending")

    # Historical/unlinked PostTask rows are untrusted migration input. Reuse the
    # same strict result/error boundary as the normal Publication bridge so malformed
    # payloads cannot stall mirroring or leak arbitrary transport text into Studio.
    ids = normalize_telegram_message_ids(payload.get("result_ids"))
    result_link = normalize_telegram_result_link(payload.get("result_link"))
    last_error = (
        public_scheduler_error(task.error) if task_status == "failed" else None
    )
    autodelete_runtime = normalize_autodelete_runtime(payload)
    repeat_group_id = _repeat_group_id(payload, task_id=task_id)
    now = datetime.now(timezone.utc)
    when = as_utc(task.scheduled_at)
    reused_content = referenced is not None

    try:
        if referenced is None:
            assert document is not None
            item = ContentItem(
                channel_id=channel_id,
                kind="post",
                status="ready" if task_status in {"pending", "processing"} else "archived",
                title=_title_from_document_text(document.primary_text()),
                current_revision=1,
                meta={"legacy_post_task_id": task_id},
            )
            session.add(item)
            await session.flush()
            revision = ContentRevision(
                content_item_id=int(item.id),
                revision=1,
                document=document.to_dict(),
                source="legacy_mirror",
                created_by_tg_user_id=_author_id(payload),
                meta={"legacy_post_task_id": task_id},
            )
            session.add(revision)
            revision_number = 1
        else:
            item, revision = referenced
            revision_number = int(revision.revision)

        mirror_meta = {"mirrored_from_legacy": True}
        if autodelete_runtime is not None:
            mirror_meta[AUTODELETE_RUNTIME_META_KEY] = autodelete_runtime
        schedule_meta: dict[str, Any] = {"legacy_post_task_id": task_id}
        if repeat_group_id is not None:
            mirror_meta["repeat_group_id"] = repeat_group_id
            schedule_meta["repeat_group_id"] = repeat_group_id
        if reused_content:
            mirror_meta["reused_content_provenance"] = True
            schedule_meta["reused_content_provenance"] = True
        if reused_repeat_root:
            mirror_meta["repeat_root_provenance"] = True
            schedule_meta["repeat_root_provenance"] = True

        schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=revision_number,
            channel_id=channel_id,
            scheduled_at=when,
            timezone=None,
            status=schedule_status,
            repeat_rule=_repeat_rule(payload),
            meta=schedule_meta,
        )
        publication = Publication(
            schedule_entry_id=None,
            content_item_id=int(item.id),
            content_revision=revision_number,
            channel_id=channel_id,
            status=publication_status,
            legacy_post_task_id=task_id,
            telegram_message_ids=ids or None,
            result_link=result_link,
            last_error=last_error,
            attempt_count=1 if task_status in _TERMINAL_STATUS else 0,
            meta=mirror_meta,
        )
        session.add_all([schedule, publication])
        await session.flush()
        publication.schedule_entry_id = int(schedule.id)

        if task_status in _TERMINAL_STATUS:
            session.add(
                PublicationAttempt(
                    publication_id=int(publication.id),
                    attempt=1,
                    status=publication_status,
                    telegram_message_ids=ids or None,
                    error=publication.last_error,
                    meta={"legacy_post_task_id": task_id, "mirrored": True},
                    finished_at=now,
                )
            )

        # Content identity and channel context are canonical in DB state. Historical
        # marker reads above remain for compatibility, but successful mirroring stops
        # perpetuating them in mutable transport JSON. Normal scheduler rich delivery
        # resolves channel from ephemeral `_post_task_id -> PostTask.channel_id`.
        payload.pop("_content_item_id", None)
        payload.pop("_content_revision", None)
        payload.pop("_content_channel_id", None)
        payload.pop("_publication_id", None)
        task.payload = payload
        await session.commit()
        await session.refresh(publication)
        return publication
    except Exception:
        await session.rollback()
        raise


async def mirror_unlinked_legacy_tasks(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> tuple[int, int]:
    """Mirror a bounded batch. Returns (mirrored, skipped)."""
    linked_subquery = select(Publication.legacy_post_task_id).where(
        Publication.legacy_post_task_id.is_not(None)
    )
    result = await session.execute(
        select(PostTask)
        .where(PostTask.id.not_in(linked_subquery))
        .order_by(PostTask.id.desc())
        .limit(max(1, min(int(limit), 500)))
    )
    tasks = list(result.scalars().all())
    mirrored = 0
    skipped = 0
    for task in reversed(tasks):
        try:
            publication = await mirror_legacy_post_task(session, task)
            if publication is None:
                skipped += 1
            else:
                mirrored += 1
        except Exception:
            skipped += 1
            logger.exception("Legacy content mirror failed PostTask id={}", int(task.id))
    return mirrored, skipped
