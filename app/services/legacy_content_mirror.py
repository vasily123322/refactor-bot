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


def _content_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = cleanup_runtime_fields(payload)
    for key in _RUNTIME_ONLY_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def _repeat_rule(payload: dict[str, Any]) -> dict[str, Any]:
    if not bool(payload.get("repeat_on", False)):
        return {}
    seconds = int(payload.get("repeat_seconds") or 0)
    return {"enabled": seconds > 0, "seconds": max(0, seconds)}


def _author_id(payload: dict[str, Any]) -> int | None:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("author_user_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _title_from_document_text(text: str) -> str | None:
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    return normalized[:120]


async def _referenced_content(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    channel_id: int,
) -> tuple[ContentItem, ContentRevision] | None:
    item_marker = payload.get("_content_item_id")
    revision_marker = payload.get("_content_revision")
    if item_marker is None or revision_marker is None:
        return None

    try:
        item_id = int(item_marker)
        revision_number = int(revision_marker)
        channel_marker = payload.get("_content_channel_id")
        if channel_marker is not None and int(channel_marker) != int(channel_id):
            return None
    except (TypeError, ValueError):
        return None

    item = await session.get(ContentItem, item_id)
    if item is None or int(item.channel_id) != int(channel_id):
        return None

    revision = (
        await session.execute(
            select(ContentRevision).where(
                ContentRevision.content_item_id == item_id,
                ContentRevision.revision == revision_number,
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        return None
    return item, revision


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
    existing = (
        await session.execute(
            select(Publication).where(Publication.legacy_post_task_id == task_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    payload = deepcopy(dict(task.payload or {}))
    publication_marker = payload.get("_publication_id")
    if publication_marker is not None:
        try:
            marked = await session.get(Publication, int(publication_marker))
        except (TypeError, ValueError):
            marked = None
        if marked is not None and int(marked.legacy_post_task_id or 0) == task_id:
            return marked
        # Old repeat payloads could inherit the parent's publication marker. Treat
        # that as stale delivery identity while retaining safe content provenance.
        payload.pop("_publication_id", None)

    referenced = await _referenced_content(
        session,
        payload,
        channel_id=channel_id,
    )
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
        schedule_meta: dict[str, Any] = {"legacy_post_task_id": task_id}
        if reused_content:
            mirror_meta["reused_content_provenance"] = True
            schedule_meta["reused_content_provenance"] = True

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

        payload["_content_item_id"] = int(item.id)
        payload["_content_revision"] = revision_number
        payload["_content_channel_id"] = channel_id
        payload["_publication_id"] = int(publication.id)
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
