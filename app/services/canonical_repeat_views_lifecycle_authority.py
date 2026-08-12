from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.canonical_repeat_continuation_authority import (
    CanonicalRepeatContinuationAuthorityService,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import normalize_telegram_message_ids


_REPEAT_VIEWS_RUNTIME_KEYS = frozenset(
    {"silent", "pin_on", "forward_to", "autodelete_views", "autodelete_report"}
)


@dataclass(frozen=True, slots=True)
class CanonicalRepeatViewsLifecycleAuthority:
    publication_id: int
    schedule_entry_id: int
    attempt: int
    repeat_group_id: int
    repeat_seconds: int
    threshold: int
    autodelete_report: bool
    telegram_message_ids: tuple[int, ...]
    runtime_options: dict[str, Any]


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


class CanonicalRepeatViewsLifecycleAuthorityService:
    """Lock and prove one terminal canonical repeat occurrence owns views lifecycle state.

    Plain repeat+views, views+pin and views+ordered-forward retain their independent facts.
    The combined views+pin+forward profile is narrower again: it requires all three proof
    facts (`allow_pin`, `allow_forward`, `allow_pin_forward`) at the same provider-free
    boundary. Independent pin+forward facts therefore never compose implicitly.

    Exact terminal canonical source/Attempt identity and the occurrence-local indexed views
    row remain locked before any later destructive consumer can observe or delete.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_and_prove(
        self,
        publication_id: int,
        *,
        allow_pin: bool = False,
        allow_forward: bool = False,
        allow_pin_forward: bool = False,
    ) -> CanonicalRepeatViewsLifecycleAuthority | None:
        authority = await CanonicalRepeatContinuationAuthorityService(
            self.session
        ).lock_and_prove(publication_id)
        if authority is None:
            return None

        runtime_options = dict(authority.runtime_options)
        if (
            "autodelete_views" not in runtime_options
            or not set(runtime_options).issubset(_REPEAT_VIEWS_RUNTIME_KEYS)
        ):
            return None
        capability = parse_canonical_publication_delivery_runtime_capability(runtime_options)
        if capability is None or capability.views_autodelete_threshold is None:
            return None
        if capability.time_autodelete_requested:
            return None

        has_pin = bool(capability.pin_on)
        has_forward = bool(capability.forward_to)
        if has_pin and has_forward:
            if not (
                bool(allow_pin)
                and bool(allow_forward)
                and bool(allow_pin_forward)
            ):
                return None
        else:
            if has_pin and not bool(allow_pin):
                return None
            if has_forward and not bool(allow_forward):
                return None

        publication_meta = _mapping(authority.publication.meta)
        if publication_meta is None or AUTODELETE_RUNTIME_META_KEY in publication_meta:
            return None

        publication_message_ids = tuple(
            normalize_telegram_message_ids(authority.publication.telegram_message_ids)
        )
        attempt_message_ids = tuple(
            normalize_telegram_message_ids(authority.attempt.telegram_message_ids)
        )
        if not publication_message_ids or publication_message_ids != attempt_message_ids:
            return None

        state = (
            await self.session.execute(
                select(PublicationAutodeleteViewState)
                .where(
                    PublicationAutodeleteViewState.publication_id
                    == int(authority.publication.id)
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            state is None
            or int(state.threshold) != int(capability.views_autodelete_threshold)
        ):
            return None

        return CanonicalRepeatViewsLifecycleAuthority(
            publication_id=int(authority.publication.id),
            schedule_entry_id=int(authority.schedule.id),
            attempt=int(authority.attempt.attempt),
            repeat_group_id=int(authority.repeat_group_id),
            repeat_seconds=int(authority.repeat_seconds),
            threshold=int(capability.views_autodelete_threshold),
            autodelete_report=bool(capability.autodelete_report),
            telegram_message_ids=publication_message_ids,
            runtime_options=runtime_options,
        )
