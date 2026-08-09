from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.rich_media_assets import RichMediaAssetError, RichMediaAssetResolver
from app.services.scheduling import as_utc
from app.services.telegram_renderer import TelegramRenderError, TelegramRenderer


class PublicationBridgeError(RuntimeError):
    pass


_TASK_TO_PUBLICATION_STATUS = {
    "pending": "queued",
    "processing": "sending",
    "done": "published",
    "failed": "failed",
    "skipped": "skipped",
    "cancelled": "cancelled",
}

_TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "skipped", "cancelled"})


def _scheduler_payload(
    document: PostDocument,
    *,
    render_document: PostDocument | None = None,
) -> dict[str, Any]:
    """Validate with the shared renderer and build a serializable scheduler payload."""
    try:
        plan = TelegramRenderer().render(render_document or document)
    except TelegramRenderError as exc:
        raise PublicationBridgeError(str(exc)) from exc

    if plan.kind == "classic":
        if plan.classic_payload is None:
            raise PublicationBridgeError("classic renderer returned no payload")
        return dict(plan.classic_payload)
    if plan.kind == "rich":
        # aiogram models and resolved Telegram file IDs are transport-edge details and
        # must never replace durable asset identities in the persisted PostDocument.
        return {
            "type": "rich_document",
            "post_document": document.to_dict(),
        }
    raise PublicationBridgeError(f"unsupported Telegram render plan: {plan.kind}")


class LegacyPublicationBridge:
    """Bridge the new content/planner domain onto the proven PostTask scheduler."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def queue(
        self,
        *,
        content_item_id: int,
        scheduled_at: datetime | None = None,
        content_revision: int | None = None,
        timezone_name: str | None = None,
        repeat_rule: Mapping[str, Any] | None = None,
        runtime_options: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Publication:
        item = await self.session.get(ContentItem, int(content_item_id))
        if item is None:
            raise PublicationBridgeError(f"content item {content_item_id} not found")

        revision_number = int(content_revision or item.current_revision or 0)
        if revision_number <= 0:
            raise PublicationBridgeError("content item has no revision to publish")

        revision_row = (
            await self.session.execute(
                select(ContentRevision).where(
                    ContentRevision.content_item_id == int(item.id),
                    ContentRevision.revision == revision_number,
                )
            )
        ).scalar_one_or_none()
        if revision_row is None:
            raise PublicationBridgeError(
                f"content revision {content_item_id}:{revision_number} not found"
            )

        document = PostDocument.from_dict(revision_row.document)
        try:
            render_document = await RichMediaAssetResolver(self.session).resolve(
                document,
                channel_id=int(item.channel_id),
            )
        except RichMediaAssetError as exc:
            raise PublicationBridgeError(str(exc)) from exc
        payload = _scheduler_payload(document, render_document=render_document)

        # Keep one canonical timestamp contract across SQLite/PostgreSQL and the
        # existing scheduler. SQLite may deserialize timezone=True columns as naive,
        # so all consumers must normalize persisted values with the same helper.
        when = as_utc(scheduled_at or datetime.now(timezone.utc))
        rule = dict(repeat_rule or {})
        if rule.get("enabled"):
            seconds = int(rule.get("seconds") or 0)
            if seconds <= 0:
                raise PublicationBridgeError("enabled repeat_rule requires positive seconds")
            payload["repeat_on"] = True
            payload["repeat_seconds"] = seconds
        elif repeat_rule is not None:
            payload["repeat_on"] = False
            payload.pop("repeat_seconds", None)

        for key, value in dict(runtime_options or {}).items():
            payload[str(key)] = value

        schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=revision_number,
            channel_id=int(item.channel_id),
            scheduled_at=when,
            timezone=timezone_name,
            status="pending",
            repeat_rule=rule,
            meta=dict(metadata or {}),
        )
        publication = Publication(
            schedule_entry_id=None,
            content_item_id=int(item.id),
            content_revision=revision_number,
            channel_id=int(item.channel_id),
            status="queued",
            meta=dict(metadata or {}),
        )
        self.session.add_all([schedule, publication])
        try:
            await self.session.flush()
            publication.schedule_entry_id = int(schedule.id)
            payload["_content_item_id"] = int(item.id)
            payload["_content_revision"] = revision_number
            payload["_content_channel_id"] = int(item.channel_id)
            payload["_publication_id"] = int(publication.id)

            task = PostTask(
                channel_id=int(item.channel_id),
                status="pending",
                payload=payload,
                dedupe_key=f"publication:{int(publication.id)}",
                scheduled_at=when,
            )
            self.session.add(task)
            await self.session.flush()
            publication.legacy_post_task_id = int(task.id)
            schedule.meta = {
                **dict(schedule.meta or {}),
                "legacy_post_task_id": int(task.id),
            }
            await self.session.commit()
            await self.session.refresh(publication)
            return publication
        except Exception:
            await self.session.rollback()
            raise

    async def reconcile(self, publication_id: int) -> Publication:
        publication = await self.session.get(Publication, int(publication_id))
        if publication is None:
            raise PublicationBridgeError(f"publication {publication_id} not found")
        if publication.legacy_post_task_id is None:
            raise PublicationBridgeError("publication has no legacy scheduler task")

        task = await self.session.get(PostTask, int(publication.legacy_post_task_id))
        if task is None:
            publication.status = "failed"
            publication.last_error = "legacy scheduler task is missing"
            await self.session.commit()
            await self.session.refresh(publication)
            return publication

        task_status = str(task.status or "pending")
        publication.status = _TASK_TO_PUBLICATION_STATUS.get(task_status, task_status)
        payload = dict(task.payload or {})
        ids = [int(value) for value in list(payload.get("result_ids") or [])]
        publication.telegram_message_ids = ids or None
        publication.result_link = payload.get("result_link")
        publication.last_error = task.error if task_status == "failed" else None

        schedule = (
            await self.session.get(ScheduleEntry, int(publication.schedule_entry_id))
            if publication.schedule_entry_id is not None
            else None
        )
        if schedule is not None:
            if task_status == "done":
                schedule.status = "completed"
            elif task_status in {"failed", "skipped", "cancelled"}:
                schedule.status = task_status

        if task_status in _TERMINAL_TASK_STATUSES and int(publication.attempt_count or 0) == 0:
            publication.attempt_count = 1
            self.session.add(
                PublicationAttempt(
                    publication_id=int(publication.id),
                    attempt=1,
                    status=publication.status,
                    telegram_message_ids=ids or None,
                    error=publication.last_error,
                    meta={"legacy_post_task_id": int(task.id)},
                    finished_at=datetime.now(timezone.utc),
                )
            )

        try:
            await self.session.commit()
            await self.session.refresh(publication)
            return publication
        except Exception:
            await self.session.rollback()
            raise

    async def reconcile_active(self, *, limit: int = 100) -> int:
        result = await self.session.execute(
            select(Publication.id)
            .where(
                Publication.legacy_post_task_id.is_not(None),
                Publication.status.in_(("queued", "sending")),
            )
            .order_by(Publication.id.asc())
            .limit(max(1, min(int(limit), 500)))
        )
        ids = [int(value) for value in result.scalars().all()]
        for publication_id in ids:
            await self.reconcile(publication_id)
        return len(ids)
