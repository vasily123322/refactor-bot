from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
    CanonicalPublicationDeliveryPlanner,
)
from app.services.scheduling import as_utc
from app.services.telegram_renderer import TelegramRenderError, TelegramRenderer


CUTOVER_META_KEY = "legacy_transport_cutover"
_CUTOVER_STATUS = "canonical_cutover"

_RETIRED = "retired"
_INELIGIBLE = "ineligible"
_CONTENTION = "contention"
_CONFLICT = "conflict"

_IDENTITY_MARKERS = {
    "_publication_id",
    "_content_item_id",
    "_content_revision",
    "_content_channel_id",
}
_FORBIDDEN_EPHEMERAL_KEYS = {
    "_via_scheduler",
    "_post_task_id",
    "_also_schedule_autodelete",
}
_NEUTRAL_BOOL_KEYS = {
    "pin_on",
    "forward_silent",
    "autodelete_report",
}
_NEUTRAL_INT_KEYS = {
    "repeat_seconds",
    "autodelete_seconds",
    "autodelete_effective_seconds",
    "autodelete_views",
}
_NEUTRAL_OPTIONAL_KEYS = {
    "autodelete_at",
    "autodeleted_at",
    "result_link",
}


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): deepcopy(item) for key, item in value.items()}


def _neutral_bool(value: Any) -> bool:
    return value is None or value is False or value == 0


def _neutral_int(value: Any) -> bool:
    return value in (None, False, 0, "0", "")


def _neutral_payload_effects(payload: Mapping[str, Any]) -> bool:
    if payload.get("repeat_on") not in (None, False, 0):
        return False
    if payload.get("repeat_group_id") is not None:
        return False
    if payload.get("forward_to") not in (None, []):
        return False
    if payload.get("result_ids") not in (None, []):
        return False
    if payload.get("autodeleted") not in (None, False):
        return False
    for key in _NEUTRAL_BOOL_KEYS:
        if key in payload and not _neutral_bool(payload.get(key)):
            return False
    for key in _NEUTRAL_INT_KEYS:
        if key in payload and not _neutral_int(payload.get(key)):
            return False
    for key in _NEUTRAL_OPTIONAL_KEYS:
        if payload.get(key) not in (None, ""):
            return False
    return True


def _strip_neutral_effect_fields(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not _neutral_payload_effects(payload):
        return None
    cleaned = deepcopy(payload)
    for key in (
        "repeat_on",
        "repeat_seconds",
        "repeat_group_id",
        "silent",
        "pin_on",
        "forward_to",
        "forward_silent",
        "autodelete_seconds",
        "autodelete_effective_seconds",
        "autodelete_views",
        "autodelete_report",
        "autodelete_at",
        "autodeleted",
        "autodeleted_at",
        "result_ids",
        "result_link",
    ):
        cleaned.pop(key, None)
    return cleaned


def _nonrepeat(plan: CanonicalPublicationDeliveryPlan) -> bool:
    try:
        rule = plan.repeat_rule()
    except (TypeError, ValueError):
        return False
    enabled = rule.get("enabled")
    return enabled is None or enabled is False


def _supported_runtime_options(
    plan: CanonicalPublicationDeliveryPlan,
) -> dict[str, Any] | None:
    try:
        options = plan.runtime_options()
    except (TypeError, ValueError):
        return None
    if not options:
        return {}
    if set(options) != {"silent"}:
        return None
    if type(options.get("silent")) is not bool:
        return None
    return options


def _expected_transport_payload(
    plan: CanonicalPublicationDeliveryPlan,
) -> dict[str, Any] | None:
    try:
        document = plan.post_document()
    except (TypeError, ValueError):
        return None

    if document.mode == "rich":
        return {
            "type": "rich_document",
            "post_document": document.to_dict(),
        }

    try:
        rendered = TelegramRenderer().render(document)
    except (TelegramRenderError, TypeError, ValueError):
        return None
    if rendered.kind != "classic" or rendered.classic_payload is None:
        return None
    return deepcopy(dict(rendered.classic_payload))


def _identity_markers_match(
    payload: dict[str, Any],
    *,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
) -> bool:
    expected = {
        "_publication_id": int(publication.id),
        "_content_item_id": int(plan.content_item_id),
        "_content_revision": int(plan.content_revision),
        "_content_channel_id": int(plan.channel_id),
    }
    for key, expected_value in expected.items():
        if key not in payload:
            continue
        raw = payload.get(key)
        if isinstance(raw, bool):
            return False
        try:
            parsed = int(raw)
        except (TypeError, ValueError, OverflowError):
            return False
        if parsed != expected_value:
            return False
    return True


def _silent_intent_matches(
    current: Mapping[str, Any],
    expected: Mapping[str, Any],
    runtime_options: Mapping[str, Any],
) -> bool:
    if "silent" in runtime_options:
        intended = runtime_options.get("silent")
        if type(intended) is not bool:
            return False
        if type(current.get("silent")) is not bool or current.get("silent") is not intended:
            return False
        if "silent" in expected:
            rendered_value = expected.get("silent")
            if type(rendered_value) is not bool or rendered_value is not intended:
                return False
        return True

    for payload in (current, expected):
        if "silent" in payload and not _neutral_bool(payload.get("silent")):
            return False
    return True


def _legacy_intent_matches(
    *,
    task: PostTask,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
) -> bool:
    if int(task.channel_id) != int(plan.channel_id):
        return False
    if task.scheduled_at is None or as_utc(task.scheduled_at) != as_utc(plan.scheduled_at):
        return False
    if task.error not in (None, ""):
        return False

    runtime_options = _supported_runtime_options(plan)
    current = _mapping(task.payload)
    expected = _expected_transport_payload(plan)
    if runtime_options is None or current is None or expected is None:
        return False
    if any(key in current for key in _FORBIDDEN_EPHEMERAL_KEYS):
        return False
    if not _identity_markers_match(current, publication=publication, plan=plan):
        return False
    if not _silent_intent_matches(current, expected, runtime_options):
        return False
    for key in _IDENTITY_MARKERS:
        current.pop(key, None)

    current_clean = _strip_neutral_effect_fields(current)
    expected_clean = _strip_neutral_effect_fields(expected)
    if current_clean is None or expected_clean is None:
        return False
    return current_clean == expected_clean


@dataclass(frozen=True, slots=True)
class CanonicalPublicationLegacyTransportHandoffResult:
    outcome: str
    publication_id: int
    legacy_post_task_id: int | None = None


class CanonicalPublicationLegacyTransportHandoffService:
    """Atomically retire one still-pending legacy transport before canonical claim.

    The `pending -> canonical_cutover` compare-and-set is the serialization seam against
    legacy `SchedulerTaskLeaseService.claim_pending()`. It happens before any unlink and
    is never committed as a visible intermediate lifecycle: success deletes PostTask in
    the same transaction, while every failure rolls the status back to `pending`.

    Any SchedulerTaskLease, live or expired, is an execution/recovery barrier and blocks
    handoff. This service never deletes or takes a scheduler lease and never calls a
    provider. Canonical delivery remains `queued` until a later exact canonical claim.

    Current capability is non-repeat with empty runtime options or explicit `silent: bool`.
    The legacy PostTask silent bit must exactly match explicit canonical silent intent;
    hidden legacy silent behavior is not inferred into canonical authority.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _result(
        self,
        outcome: str,
        publication_id: int,
        task_id: int | None = None,
    ) -> CanonicalPublicationLegacyTransportHandoffResult:
        await self.session.rollback()
        return CanonicalPublicationLegacyTransportHandoffResult(
            outcome=outcome,
            publication_id=int(publication_id),
            legacy_post_task_id=(int(task_id) if task_id is not None else None),
        )

    async def retire_for_canonical_delivery(
        self,
        publication_id: int,
        *,
        at: datetime | None = None,
    ) -> CanonicalPublicationLegacyTransportHandoffResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_publication_id = 0
        if safe_publication_id <= 0:
            return CanonicalPublicationLegacyTransportHandoffResult(
                outcome=_INELIGIBLE,
                publication_id=safe_publication_id,
            )
        current = as_utc(at or datetime.now(timezone.utc))

        try:
            publication = (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.id == safe_publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                publication is None
                or publication.status != "queued"
                or int(publication.attempt_count or 0) != 0
                or publication.legacy_post_task_id is None
                or publication.telegram_message_ids not in (None, [])
                or publication.result_link is not None
                or publication.last_error is not None
            ):
                return await self._result(_INELIGIBLE, safe_publication_id)
            task_id = int(publication.legacy_post_task_id)

            claimed_cutover = await self.session.execute(
                update(PostTask)
                .where(
                    PostTask.id == task_id,
                    PostTask.status == "pending",
                )
                .values(status=_CUTOVER_STATUS)
                .execution_options(synchronize_session=False)
            )
            if int(claimed_cutover.rowcount or 0) != 1:
                return await self._result(_CONTENTION, safe_publication_id, task_id)

            task = (
                await self.session.execute(
                    select(PostTask)
                    .where(PostTask.id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None or str(task.status) != _CUTOVER_STATUS:
                return await self._result(_CONFLICT, safe_publication_id, task_id)

            scheduler_lease = (
                await self.session.execute(
                    select(SchedulerTaskLease)
                    .where(SchedulerTaskLease.task_id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if scheduler_lease is not None:
                return await self._result(_CONFLICT, safe_publication_id, task_id)

            canonical_lease = (
                await self.session.execute(
                    select(PublicationDeliveryLease)
                    .where(PublicationDeliveryLease.publication_id == safe_publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if canonical_lease is not None:
                return await self._result(_CONFLICT, safe_publication_id, task_id)

            if publication.schedule_entry_id is None:
                return await self._result(_INELIGIBLE, safe_publication_id, task_id)
            schedule = (
                await self.session.execute(
                    select(ScheduleEntry)
                    .where(ScheduleEntry.id == int(publication.schedule_entry_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            item = (
                await self.session.execute(
                    select(ContentItem)
                    .where(ContentItem.id == int(publication.content_item_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            revision = (
                await self.session.execute(
                    select(ContentRevision)
                    .where(
                        ContentRevision.content_item_id == int(publication.content_item_id),
                        ContentRevision.revision == int(publication.content_revision),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            channel = (
                await self.session.execute(
                    select(Channel)
                    .where(Channel.id == int(publication.channel_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None or item is None or revision is None or channel is None:
                return await self._result(_INELIGIBLE, safe_publication_id, task_id)

            plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
                safe_publication_id,
                at=current,
            )
            if (
                plan is None
                or int(plan.schedule_entry_id) != int(schedule.id)
                or int(plan.content_item_id) != int(item.id)
                or int(plan.content_revision) != int(revision.revision)
                or int(plan.channel_id) != int(channel.id)
                or _supported_runtime_options(plan) is None
                or not _nonrepeat(plan)
                or not _legacy_intent_matches(
                    task=task,
                    publication=publication,
                    plan=plan,
                )
            ):
                return await self._result(_INELIGIBLE, safe_publication_id, task_id)

            schedule_meta = _mapping(schedule.meta)
            publication_meta = _mapping(publication.meta)
            if schedule_meta is None or publication_meta is None:
                return await self._result(_INELIGIBLE, safe_publication_id, task_id)
            legacy_marker = schedule_meta.get("legacy_post_task_id")
            if legacy_marker is not None:
                if isinstance(legacy_marker, bool):
                    return await self._result(_CONFLICT, safe_publication_id, task_id)
                try:
                    if int(legacy_marker) != task_id:
                        return await self._result(_CONFLICT, safe_publication_id, task_id)
                except (TypeError, ValueError, OverflowError):
                    return await self._result(_CONFLICT, safe_publication_id, task_id)
                schedule_meta.pop("legacy_post_task_id", None)

            cutover_meta = {
                "retired": True,
                "retired_at": current.isoformat(),
                "legacy_post_task_id": task_id,
                "source_status": "pending",
            }
            schedule.meta = {
                **schedule_meta,
                CUTOVER_META_KEY: deepcopy(cutover_meta),
            }
            publication.meta = {
                **publication_meta,
                CUTOVER_META_KEY: deepcopy(cutover_meta),
            }
            publication.legacy_post_task_id = None
            await self.session.delete(task)
            await self.session.commit()
            return CanonicalPublicationLegacyTransportHandoffResult(
                outcome=_RETIRED,
                publication_id=safe_publication_id,
                legacy_post_task_id=task_id,
            )
        except Exception:
            await self.session.rollback()
            raise
