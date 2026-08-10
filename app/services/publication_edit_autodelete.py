from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateError,
    PublicationAutodeleteViewStateService,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import normalize_telegram_message_ids


class PublicationEditAutodeleteSyncError(RuntimeError):
    pass


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PublicationEditAutodeleteSyncError(f"{label} is malformed")
    return deepcopy(dict(value))


def _option_int(options: Mapping[str, Any], key: str) -> int | None:
    raw = options.get(key)
    if raw in (None, "", 0, "0", False):
        return None
    if isinstance(raw, bool):
        raise PublicationEditAutodeleteSyncError(f"invalid canonical runtime option: {key}")
    try:
        parsed = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PublicationEditAutodeleteSyncError(
            f"invalid canonical runtime option: {key}"
        ) from exc
    if parsed <= 0:
        raise PublicationEditAutodeleteSyncError(f"invalid canonical runtime option: {key}")
    return parsed


def _option_report(options: Mapping[str, Any]) -> bool:
    value = options.get("autodelete_report")
    if value is None or value is False:
        return False
    if value is not True:
        raise PublicationEditAutodeleteSyncError(
            "invalid canonical runtime option: autodelete_report"
        )
    return True


def validate_publication_edit_autodelete_execution(
    *,
    legacy_post_task_id: int | None,
    runtime_options: Mapping[str, Any],
) -> None:
    """Reject canonical editor intent that has no proven executor after retirement."""
    # Views execution is now proven for canonical-only Publications. Keep validating
    # its shape here, but no longer require the legacy PostTask transport.
    _option_int(runtime_options, "autodelete_views")
    report = _option_report(runtime_options)
    if legacy_post_task_id is None and report:
        raise PublicationEditAutodeleteSyncError(
            "autodelete report requires legacy transport"
        )


def _runtime_state(meta: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = meta.get(AUTODELETE_RUNTIME_META_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PublicationEditAutodeleteSyncError(
            "canonical autodelete runtime is malformed"
        )
    return deepcopy(dict(raw))


def _runtime_due_for_seconds(
    runtime: Mapping[str, Any] | None,
    seconds: int,
) -> str | None:
    if runtime is None or runtime.get("deleted") is True:
        return None
    try:
        effective = int(runtime.get("effective_seconds") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    due = runtime.get("scheduled_at")
    if effective != int(seconds) or not isinstance(due, str) or not due.strip():
        return None
    return due.strip()


def _set_runtime(publication: Publication, runtime: dict[str, Any] | None) -> None:
    meta = _mapping(publication.meta, label="canonical publication metadata")
    if runtime is None:
        meta.pop(AUTODELETE_RUNTIME_META_KEY, None)
    else:
        meta[AUTODELETE_RUNTIME_META_KEY] = deepcopy(runtime)
    publication.meta = meta


class PublicationEditAutodeleteSyncService:
    """Synchronize post-publication runtime and compatibility delivery state.

    Canonical metadata is authoritative. A still-linked `PostTask` is updated as a
    compatibility executor. Generated timer state is reset only when timer/views intent
    changes or when linked transport is missing equivalent timer state; ordinary text
    edits preserve the original due time. Confirmed Telegram delivery IDs/link are also
    mirrored so legacy autodelete targets the message that actually survived the edit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def apply(
        self,
        *,
        publication: Publication,
        schedule: ScheduleEntry,
        previous_runtime_options: Mapping[str, Any],
        runtime_options: Mapping[str, Any],
        telegram_message_ids: list[int] | tuple[int, ...],
        result_link: str | None,
        now: datetime | None = None,
    ) -> None:
        previous = _mapping(previous_runtime_options, label="previous runtime options")
        current = _mapping(runtime_options, label="runtime options")
        ids = normalize_telegram_message_ids(list(telegram_message_ids))
        if not ids:
            raise PublicationEditAutodeleteSyncError(
                "confirmed canonical edit has invalid delivery ids"
            )

        previous_seconds = _option_int(previous, "autodelete_seconds")
        current_seconds = _option_int(current, "autodelete_seconds")
        previous_views = _option_int(previous, "autodelete_views")
        current_views = _option_int(current, "autodelete_views")
        current_report = _option_report(current)
        if current_seconds is not None and current_views is not None:
            raise PublicationEditAutodeleteSyncError(
                "canonical autodelete timer and views are mutually exclusive"
            )

        raw_legacy_id = publication.legacy_post_task_id
        legacy_id = int(raw_legacy_id) if raw_legacy_id is not None else None
        validate_publication_edit_autodelete_execution(
            legacy_post_task_id=legacy_id,
            runtime_options=current,
        )

        current_time = _utc(now)
        timer_changed = previous_seconds != current_seconds
        views_changed = previous_views != current_views
        runtime = _runtime_state(_mapping(publication.meta, label="publication metadata"))
        due_token = (
            _runtime_due_for_seconds(runtime, current_seconds)
            if current_seconds is not None
            else None
        )

        task: PostTask | None = None
        task_payload: dict[str, Any] | None = None
        task_timer_changed = False
        task_views_changed = False
        if legacy_id is not None:
            task = (
                await self.session.execute(
                    select(PostTask)
                    .where(PostTask.id == legacy_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None:
                raise PublicationEditAutodeleteSyncError(
                    "linked legacy transport is missing"
                )
            if int(task.channel_id) != int(publication.channel_id) or str(task.status) != "done":
                raise PublicationEditAutodeleteSyncError(
                    "linked legacy transport is not consistently published"
                )
            task_payload = _mapping(task.payload, label="legacy transport payload")
            task_timer_changed = (
                _option_int(task_payload, "autodelete_seconds") != current_seconds
            )
            task_views_changed = (
                _option_int(task_payload, "autodelete_views") != current_views
            )

        generated_timer_change = timer_changed or task_timer_changed
        generated_views_change = views_changed or task_views_changed

        if current_seconds is not None and generated_timer_change:
            if not timer_changed and due_token is not None:
                due = due_token
            else:
                due = (current_time + timedelta(seconds=current_seconds)).isoformat()
                runtime = {
                    "effective_seconds": current_seconds,
                    "scheduled_at": due,
                    "deleted": False,
                }
                _set_runtime(publication, runtime)
        elif current_seconds is None and (generated_timer_change or generated_views_change):
            due = None
            _set_runtime(publication, None)
        else:
            due = due_token

        try:
            await PublicationAutodeleteViewStateService(self.session).sync_intent(
                publication_id=int(publication.id),
                threshold=current_views,
                now=current_time,
            )
        except PublicationAutodeleteViewStateError as exc:
            raise PublicationEditAutodeleteSyncError(str(exc)) from None

        if task is None or task_payload is None:
            return

        task_payload["result_ids"] = list(ids)
        if result_link is None:
            task_payload.pop("result_link", None)
        else:
            task_payload["result_link"] = str(result_link)

        for key in ("autodelete_seconds", "autodelete_views", "autodelete_report"):
            task_payload.pop(key, None)
        if current_seconds is not None:
            task_payload["autodelete_seconds"] = current_seconds
        if current_views is not None:
            task_payload["autodelete_views"] = current_views
        if current_report:
            task_payload["autodelete_report"] = True

        if generated_timer_change:
            task_payload.pop("autodeleted", None)
            task_payload.pop("autodeleted_at", None)
            if current_seconds is None:
                task_payload.pop("autodelete_effective_seconds", None)
                task_payload.pop("autodelete_at", None)
            else:
                task_payload["autodelete_effective_seconds"] = current_seconds
                task_payload["autodelete_at"] = due or (
                    current_time + timedelta(seconds=current_seconds)
                ).isoformat()
        if generated_views_change and current_seconds is None:
            task_payload.pop("autodelete_effective_seconds", None)
            task_payload.pop("autodelete_at", None)
            task_payload.pop("autodeleted", None)
            task_payload.pop("autodeleted_at", None)

        task.payload = task_payload
