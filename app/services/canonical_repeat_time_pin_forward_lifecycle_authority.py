from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.canonical_repeat_continuation_authority import (
    CanonicalRepeatContinuationAuthorityService,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.scheduling import as_utc
from app.services.telegram_results import normalize_telegram_message_ids


_ALLOWED_RUNTIME_KEYS = frozenset(
    {
        "silent",
        "pin_on",
        "forward_to",
        "autodelete_seconds",
        "autodelete_report",
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalRepeatTimePinForwardLifecycleAuthority:
    publication_id: int
    schedule_entry_id: int
    attempt: int
    repeat_group_id: int
    repeat_seconds: int
    time_autodelete_seconds: int
    scheduled_at: datetime
    autodelete_report: bool
    telegram_message_ids: tuple[int, ...]
    runtime_options: dict[str, Any]
    pin_on: bool
    forward_channel_ids: tuple[int, ...]


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _runtime_scheduled_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return as_utc(datetime.fromisoformat(text))
    except (TypeError, ValueError, OverflowError):
        return None


class CanonicalRepeatTimePinForwardLifecycleAuthorityService:
    """Provider-free terminal proof for exact repeat+time+pin+forward state."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_and_prove(
        self,
        publication_id: int,
        *,
        allow_time_pin_forward: bool = False,
    ) -> CanonicalRepeatTimePinForwardLifecycleAuthority | None:
        if not bool(allow_time_pin_forward):
            return None

        authority = await CanonicalRepeatContinuationAuthorityService(
            self.session
        ).lock_and_prove(publication_id)
        if authority is None:
            return None

        runtime_options = dict(authority.runtime_options)
        if (
            "autodelete_seconds" not in runtime_options
            or runtime_options.get("pin_on") is not True
            or "forward_to" not in runtime_options
            or not set(runtime_options).issubset(_ALLOWED_RUNTIME_KEYS)
        ):
            return None
        capability = parse_canonical_publication_delivery_runtime_capability(
            runtime_options
        )
        if (
            capability is None
            or capability.time_autodelete_seconds is None
            or capability.pin_on is not True
            or not capability.forward_to
            or capability.views_autodelete_requested
        ):
            return None
        seconds = int(capability.time_autodelete_seconds)
        if seconds <= 0:
            return None

        publication_message_ids = tuple(
            normalize_telegram_message_ids(authority.publication.telegram_message_ids)
        )
        attempt_message_ids = tuple(
            normalize_telegram_message_ids(authority.attempt.telegram_message_ids)
        )
        if not publication_message_ids or publication_message_ids != attempt_message_ids:
            return None

        publication_meta = _mapping(authority.publication.meta)
        if publication_meta is None:
            return None
        runtime = _mapping(publication_meta.get(AUTODELETE_RUNTIME_META_KEY))
        if runtime is None or set(runtime) != {
            "deleted",
            "effective_seconds",
            "scheduled_at",
        }:
            return None
        if runtime.get("deleted") is not False:
            return None
        raw_effective_seconds = runtime.get("effective_seconds")
        if isinstance(raw_effective_seconds, bool):
            return None
        try:
            effective_seconds = int(raw_effective_seconds)
        except (TypeError, ValueError, OverflowError):
            return None
        if effective_seconds != seconds:
            return None

        due_at = _runtime_scheduled_at(runtime.get("scheduled_at"))
        if due_at is None or authority.attempt.finished_at is None:
            return None
        earliest_due = as_utc(authority.attempt.finished_at) + timedelta(seconds=seconds)
        if due_at < earliest_due:
            return None

        return CanonicalRepeatTimePinForwardLifecycleAuthority(
            publication_id=int(authority.publication.id),
            schedule_entry_id=int(authority.schedule.id),
            attempt=int(authority.attempt.attempt),
            repeat_group_id=int(authority.repeat_group_id),
            repeat_seconds=int(authority.repeat_seconds),
            time_autodelete_seconds=seconds,
            scheduled_at=due_at,
            autodelete_report=bool(capability.autodelete_report),
            telegram_message_ids=publication_message_ids,
            runtime_options=runtime_options,
            pin_on=True,
            forward_channel_ids=tuple(int(value) for value in capability.forward_to),
        )
