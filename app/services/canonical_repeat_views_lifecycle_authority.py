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
    {"silent", "pin_on", "autodelete_views", "autodelete_report"}
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

    This provider-free prerequisite composes the centralized repeat-continuation authority
    proof from the TOCTOU fix instead of creating another repeat-origin policy. Plain/
    silent repeat+views remains the default profile. The already-parity-proven views+pin
    lifecycle can be inspected only with the explicit `allow_pin` proof fact; production
    callers that do not pass it remain fail-closed.

    Ordered forward and all time-autodelete compositions remain outside this proof. The
    indexed views row is occurrence-local and locked in the same transaction, while exact
    source/Attempt Telegram message identity is required before any later destructive
    consumer can bind observations/deletes to this canonical delivery.

    No Telegram call, lease acquisition, candidate selection or destructive authority is
    granted by this service.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_and_prove(
        self,
        publication_id: int,
        *,
        allow_pin: bool = False,
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
        if (
            capability is None
            or capability.views_autodelete_threshold is None
            or capability.time_autodelete_requested
            or capability.forward_to
            or (capability.pin_on and not bool(allow_pin))
        ):
            return None

        publication_meta = _mapping(authority.publication.meta)
        if publication_meta is None or AUTODELETE_RUNTIME_META_KEY in publication_meta:
            # Queue-time intent is valid here; generated/deleted lifecycle state is not.
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