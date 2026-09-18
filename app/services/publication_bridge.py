from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    CANONICAL_SCHEDULING_OUTCOME,
    LEGACY_ALLOWLISTED_SCHEDULING_OUTCOME,
    scheduling_boundary_from_runtime_options,
)
from app.services.rich_media_assets import RichMediaAssetError, RichMediaAssetResolver
from app.services.scheduling import as_utc
from app.services.telegram_renderer import TelegramRenderError, TelegramRenderer


class PublicationBridgeError(RuntimeError):
    pass


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
    Deep copies keep ScheduleEntry, Publication and render payload state independent.
    """
    meta = deepcopy(dict(metadata or {}))
    meta.pop("runtime_options", None)
    runtime_intent = deepcopy(dict(runtime_options or {}))
    if runtime_intent:
        meta["runtime_options"] = runtime_intent
    return meta


class LegacyPublicationBridge:
    """Compatibility-named facade that queues canonical Publication/ScheduleEntry rows."""

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
        boundary = scheduling_boundary_from_runtime_options(runtime_intent)
        execution_mode = boundary.execution_mode
        if boundary.outcome == LEGACY_ALLOWLISTED_SCHEDULING_OUTCOME:
            raise PublicationBridgeError(
                "legacy PostTask creation is frozen"
            )
        if boundary.outcome != CANONICAL_SCHEDULING_OUTCOME or execution_mode is None:
            raise PublicationBridgeError(
                f"unsupported scheduling profile: {boundary.reason}"
            )
        if (
            rule.get("enabled")
            and boundary.runtime_options is not None
            and "autodelete_seconds" in boundary.runtime_options
            and "autodelete_views" in boundary.runtime_options
        ):
            # #509 proves shared mixed destructive authority for the ordinary canonical
            # occurrence. Repeat mixed remains explicitly closed until its dedicated
            # repeat workers are converged; never fall back to a fresh PostTask.
            raise PublicationBridgeError(
                "unsupported scheduling profile: unsupported_repeat_mixed_time_views"
            )
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
            execution_mode=execution_mode,
            meta=deepcopy(canonical_meta),
        )
        self.session.add_all([schedule, publication])
        try:
            await self.session.flush()
            publication.schedule_entry_id = int(schedule.id)
            if execution_mode != CANONICAL_EXECUTION_MODE:
                raise PublicationBridgeError(
                    "canonical scheduling outcome has non-canonical execution mode"
                )
            if rule.get("enabled"):
                repeat_group_id = int(publication.id)
                schedule.meta = {
                    **deepcopy(dict(schedule.meta or {})),
                    "repeat_group_id": repeat_group_id,
                }
                publication.meta = {
                    **deepcopy(dict(publication.meta or {})),
                    "repeat_group_id": repeat_group_id,
                }
            await self.session.commit()
            await self.session.refresh(publication)
            return publication
        except Exception:
            await self.session.rollback()
            raise

