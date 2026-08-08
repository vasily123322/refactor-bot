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
from app.services.scheduling import as_utc, cleanup_runtime_fields


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


async def mirror_legacy_post_task(
    session: AsyncSession,
    task: PostTask,
) -> Publication | None:
    """Idempotently mirror one legacy PostTask into the new content domain.

    The legacy task remains the delivery source of truth during migration. A mirror
    failure must never alter task status or prevent the existing scheduler from
    processing it.
    """
    task_id = int(task.id)
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
        marked = await session.get(Publication, int(publication_marker))
        if marked is not None:
            return marked

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

    ids = [int(value) for value in list(payload.get("result_ids") or [])]
    result_link = payload.get("result_link")
    now = datetime.now(timezone.utc)
    when = as_utc(task.scheduled_at)

    item = ContentItem(
        channel_id=int(task.channel_id),
        kind="post",
        status="ready" if task_status in {"pending", "processing"} else "archived",
        title=_title_from_document_text(document.primary_text()),
        current_revision=1,
        meta={"legacy_post_task_id": task_id},
    )
    session.add(item)

    try:
        await session.flush()
        revision = ContentRevision(
            content_item_id=int(item.id),
            revision=1,
            document=document.to_dict(),
            source="legacy_mirror",
            created_by_tg_user_id=_author_id(payload),
            meta={"legacy_post_task_id": task_id},
        )
        schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=1,
            channel_id=int(task.channel_id),
            scheduled_at=when,
            timezone=None,
            status=schedule_status,
            repeat_rule=_repeat_rule(payload),
            meta={"legacy_post_task_id": task_id},
        )
        publication = Publication(
            schedule_entry_id=None,
            content_item_id=int(item.id),
            content_revision=1,
            channel_id=int(task.channel_id),
            status=publication_status,
            legacy_post_task_id=task_id,
            telegram_message_ids=ids or None,
            result_link=result_link,
            last_error=task.error if task_status == "failed" else None,
            attempt_count=1 if task_status in _TERMINAL_STATUS else 0,
            meta={"mirrored_from_legacy": True},
        )
        session.add_all([revision, schedule, publication])
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
        payload["_content_revision"] = 1
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
