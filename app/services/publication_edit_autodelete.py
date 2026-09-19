from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
    runtime_options: Mapping[str, Any],
) -> None:
    """Validate canonical editor autodelete intent against proven executors."""
    _option_int(runtime_options, "autodelete_views")
    _option_report(runtime_options)


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
    """Synchronize canonical post-publication autodelete runtime state."""

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
        if current_seconds is not None and current_views is not None:
            raise PublicationEditAutodeleteSyncError(
                "canonical autodelete timer and views are mutually exclusive"
            )

        validate_publication_edit_autodelete_execution(
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

        generated_timer_change = timer_changed
        generated_views_change = views_changed

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

        return
