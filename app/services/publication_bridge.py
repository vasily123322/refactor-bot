from __future__ import annotations

from copy import deepcopy
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
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


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
_RUNTIME_RESERVED_KEYS = frozenset(
    {
        "repeat_on",
        "repeat_seconds",
        "result_ids",
        "result_link",
        "autodelete_at",
        "autodelete_effective_seconds",
        "autodeleted",
        "autodeleted_at",
        "autosign_applied",
        "repeat_group_id",
    }
)


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


def _runtime_intent(
    runtime_options: Mapping[str, Any] | None,
    *,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep extension options while protecting canonical/transport-owned fields."""
    intent: dict[str, Any] = {}
    existing_payload_keys = {str(key) for key in payload}
    for raw_key, value in dict(runtime_options or {}).items():
        key = str(raw_key)
        if (
            not key
            or key.startswith("_")
            or key in existing_payload_keys
            or key in _RUNTIME_RESERVED_KEYS
        ):
            raise PublicationBridgeError(f"runtime option is reserved: {key or '<empty>'}")
        intent[key] = deepcopy(value)
    return intent


def _delivery_meta(
    metadata: Mapping[str, Any] | None,
    runtime_options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build durable new-domain metadata from caller intent only.

    `runtime_options` is a reserved canonical field. It records the options supplied
    at queue time, before the legacy scheduler adds result/runtime-generated fields.
    Deep copies keep ScheduleEntry, Publication and PostTask payload independent.
    """
    meta = deepcopy(dict(metadata or {}))
    meta.pop("runtime_options", None)
    runtime_intent = deepcopy(dict(runtime_options or {}))
    if runtime_intent:
        meta["runtime_options"] = runtime_intent
    return meta


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

        runtime_intent = _runtime_intent(runtime_options, payload=payload)
        for key, value in runtime_intent.items():
            payload[key] = deepcopy(value)

        canonical_meta = _delivery_meta(metadata, runtime_intent)
        schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=revision_number,
            channel_id=int(item.channel_id),
            scheduled_at=when,
            timezone=timezone_name,
            status="pending",
            repeat_rule=rule,
            meta=deepcopy(canonical_meta),
        )
        publication = Publication(
            schedule_entry_id=None,
            content_item_id=int(item.id),
            content_revision=revision_number,
            channel_id=int(item.channel_id),
            status="queued",
            meta=deepcopy(canonical_meta),
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
                **deepcopy(dict(schedule.meta or {})),
                "legacy_post_task_id": int(task.id),
            }
            await self.session.commit()
            await self.session.refresh(publication)
            return publication
        except Exception:
            await self.session.rollback()
            raise

    async def _latest_attempt(
        self,
        publication_id: int,
    ) -> PublicationAttempt | None:
        return (
            await self.session.execute(
                select(PublicationAttempt)
                .where(PublicationAttempt.publication_id == int(publication_id))
                .order_by(PublicationAttempt.attempt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _sync_attempt(
        self,
        *,
        publication: Publication,
        task: PostTask,
        task_status: str,
        ids: list[int],
    ) -> None:
        if task_status != "processing" and task_status not in _TERMINAL_TASK_STATUSES:
            return

        latest = await self._latest_attempt(int(publication.id))
        recorded_count = max(0, int(publication.attempt_count or 0))
        if latest is not None:
            recorded_count = max(recorded_count, int(latest.attempt))
            publication.attempt_count = recorded_count

        if task_status == "processing":
            if latest is not None and latest.finished_at is None:
                latest.status = "sending"
                latest.error = None
                return

            attempt_number = (
                int(latest.attempt) + 1
                if latest is not None
                else max(1, recorded_count)
            )
            publication.attempt_count = max(recorded_count, attempt_number)
            self.session.add(
                PublicationAttempt(
                    publication_id=int(publication.id),
                    attempt=attempt_number,
                    status="sending",
                    telegram_message_ids=None,
                    error=None,
                    meta={"legacy_post_task_id": int(task.id)},
                    finished_at=None,
                )
            )
            return

        terminal_status = str(publication.status)
        terminal_error = publication.last_error
        finished_at = datetime.now(timezone.utc)

        if latest is None:
            attempt_number = max(1, recorded_count)
            publication.attempt_count = max(recorded_count, attempt_number)
            self.session.add(
                PublicationAttempt(
                    publication_id=int(publication.id),
                    attempt=attempt_number,
                    status=terminal_status,
                    telegram_message_ids=ids or None,
                    error=terminal_error,
                    meta={"legacy_post_task_id": int(task.id)},
                    finished_at=finished_at,
                )
            )
            return

        if latest.finished_at is None:
            latest.status = terminal_status
            latest.telegram_message_ids = ids or None
            latest.error = terminal_error
            latest.finished_at = finished_at
            return

        if str(latest.status) == terminal_status:
            # Recovery may learn result IDs/error after the first terminal projection.
            # Enrich the same immutable attempt identity instead of duplicating it.
            latest.telegram_message_ids = ids or None
            latest.error = terminal_error
            return

        # A terminal task state changed after an already-finished attempt. Preserve
        # append-only attempt history rather than rewriting the previous outcome.
        attempt_number = int(latest.attempt) + 1
        publication.attempt_count = max(recorded_count, attempt_number)
        self.session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=attempt_number,
                status=terminal_status,
                telegram_message_ids=ids or None,
                error=terminal_error,
                meta={"legacy_post_task_id": int(task.id), "recovered_transition": True},
                finished_at=finished_at,
            )
        )

    async def _apply_task_state(
        self,
        publication: Publication,
        task: PostTask,
    ) -> Publication:
        task_status = str(task.status or "pending")
        publication.status = _TASK_TO_PUBLICATION_STATUS.get(task_status, task_status)
        payload = dict(task.payload or {})
        ids = normalize_telegram_message_ids(payload.get("result_ids"))
        publication.telegram_message_ids = ids or None
        publication.result_link = normalize_telegram_result_link(payload.get("result_link"))
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

        await self._sync_attempt(
            publication=publication,
            task=task,
            task_status=task_status,
            ids=ids,
        )

        try:
            await self.session.commit()
            await self.session.refresh(publication)
            return publication
        except Exception:
            await self.session.rollback()
            raise

    async def reconcile_task(self, task: PostTask) -> Publication | None:
        """Project one scheduler task into its linked Publication, if any.

        Linkage is resolved from Publication.legacy_post_task_id instead of trusting
        payload markers. This makes synchronous scheduler projection safe for old
        repeat rows that may still carry stale `_publication_id` values.
        """
        publication = (
            await self.session.execute(
                select(Publication).where(
                    Publication.legacy_post_task_id == int(task.id)
                )
            )
        ).scalar_one_or_none()
        if publication is None:
            return None
        return await self._apply_task_state(publication, task)

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

        return await self._apply_task_state(publication, task)

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
