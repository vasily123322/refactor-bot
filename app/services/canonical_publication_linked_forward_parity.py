from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel, PostTask
from app.domain.publishing.models import Publication
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
)
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    _FORBIDDEN_EPHEMERAL_KEYS,
    _IDENTITY_MARKERS,
    _expected_transport_payload,
    _identity_markers_match,
    _mapping,
    _nonrepeat,
    _silent_intent_matches,
    _strip_neutral_effect_fields,
)
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalPublicationLinkedForwardTargetProof:
    channel_id: int
    telegram_chat_id: int


@dataclass(frozen=True, slots=True)
class CanonicalPublicationLinkedForwardParityProof:
    publication_id: int
    source_channel_id: int
    source_telegram_chat_id: int
    forward_channel_ids: tuple[int, ...]
    forward_targets: tuple[CanonicalPublicationLinkedForwardTargetProof, ...]
    disable_notification: bool


def _neutral_number(value: Any) -> bool:
    return value in (None, False, 0, "0", "")


def _forward_only_profile(options: dict[str, Any]) -> tuple[tuple[int, ...], bool] | None:
    capability = parse_canonical_publication_delivery_runtime_capability(options)
    if capability is None or not capability.forward_to:
        return None
    if capability.pin_on or capability.time_autodelete_requested:
        # Pin/timer composition is proven separately. This seam intentionally establishes
        # forward semantics in isolation before those profiles are combined atomically.
        return None
    if options.get("autodelete_views") not in (None, False, 0, "0", ""):
        return None
    if options.get("autodelete_report") not in (None, False):
        return None
    return capability.forward_to, capability.forward_silent


def _legacy_forward_intent_matches(
    *,
    task: PostTask,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
    forward_ids: tuple[int, ...],
) -> bool:
    if int(task.channel_id) != int(plan.channel_id):
        return False
    if task.scheduled_at is None or as_utc(task.scheduled_at) != as_utc(plan.scheduled_at):
        return False
    if task.error not in (None, ""):
        return False

    current = _mapping(task.payload)
    expected = _expected_transport_payload(plan)
    if current is None or expected is None:
        return False
    if any(key in current for key in _FORBIDDEN_EPHEMERAL_KEYS):
        return False
    if not _identity_markers_match(current, publication=publication, plan=plan):
        return False

    try:
        runtime_options = plan.runtime_options()
    except (TypeError, ValueError):
        return False
    if not _silent_intent_matches(current, expected, runtime_options):
        return False

    raw_forward = current.get("forward_to")
    if not isinstance(raw_forward, list):
        return False
    if len(raw_forward) != len(forward_ids):
        return False
    normalized: list[int] = []
    for raw in raw_forward:
        if isinstance(raw, bool):
            return False
        try:
            channel_id = int(raw)
        except (TypeError, ValueError, OverflowError):
            return False
        normalized.append(channel_id)
    if tuple(normalized) != forward_ids:
        # Order is provider-visible because legacy iterates targets in list order.
        return False

    # Generated/legacy execution evidence must still be pristine before authority moves.
    if current.get("result_ids") not in (None, []):
        return False
    if current.get("result_link") not in (None, ""):
        return False
    if current.get("autodelete_at") not in (None, ""):
        return False
    if current.get("autodelete_effective_seconds") not in (None, False, 0, "0", ""):
        return False
    if current.get("autodeleted") not in (None, False):
        return False
    if current.get("autodeleted_at") not in (None, ""):
        return False

    current_clean = deepcopy(current)
    for key in _IDENTITY_MARKERS:
        current_clean.pop(key, None)
    current_clean.pop("forward_to", None)

    # A forward-only proof cannot silently consume pin/timer/repeat effects.
    if current_clean.get("pin_on") not in (None, False, 0):
        return False
    if current_clean.get("repeat_on") not in (None, False, 0):
        return False
    if not _neutral_number(current_clean.get("autodelete_seconds")):
        return False
    if not _neutral_number(current_clean.get("autodelete_views")):
        return False
    if current_clean.get("autodelete_report") not in (None, False):
        return False

    stripped_current = _strip_neutral_effect_fields(current_clean)
    stripped_expected = _strip_neutral_effect_fields(deepcopy(expected))
    return bool(
        stripped_current is not None
        and stripped_expected is not None
        and stripped_current == stripped_expected
    )


class CanonicalPublicationLinkedForwardParityService:
    """Read-only proof that linked legacy and canonical forward behavior are equivalent.

    Legacy Scheduler and canonical runtime both interpret `forward_to` as ordered internal
    Channel IDs, resolve each to its current `tg_chat_id`, then forward primary message
    ids target-major/message-major with `disable_notification == silent`.

    This service deliberately performs no handoff or claim. It makes the forward parity
    contract independently testable before integration into the atomic authority seam.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def prove(
        self,
        *,
        task: PostTask,
        publication: Publication,
        plan: CanonicalPublicationDeliveryPlan,
    ) -> CanonicalPublicationLinkedForwardParityProof | None:
        if not _nonrepeat(plan):
            return None
        try:
            options = plan.runtime_options()
        except (TypeError, ValueError):
            return None
        profile = _forward_only_profile(options)
        if profile is None:
            return None
        forward_ids, forward_silent = profile
        if not _legacy_forward_intent_matches(
            task=task,
            publication=publication,
            plan=plan,
            forward_ids=forward_ids,
        ):
            return None

        statement = select(Channel).where(Channel.id.in_(list(forward_ids)))
        rows = (await self.session.execute(statement)).scalars().all()
        by_id = {int(channel.id): channel for channel in rows}
        if len(by_id) != len(forward_ids):
            # Canonical claim is stricter than legacy's later best-effort skip. Missing
            # targets therefore remain a clean pre-claim rejection, never a changed send.
            return None

        targets: list[CanonicalPublicationLinkedForwardTargetProof] = []
        telegram_ids: set[int] = set()
        for channel_id in forward_ids:
            channel = by_id.get(channel_id)
            if channel is None:
                return None
            try:
                telegram_chat_id = int(channel.tg_chat_id)
            except (TypeError, ValueError, OverflowError):
                return None
            if telegram_chat_id == 0 or telegram_chat_id in telegram_ids:
                return None
            telegram_ids.add(telegram_chat_id)
            targets.append(
                CanonicalPublicationLinkedForwardTargetProof(
                    channel_id=channel_id,
                    telegram_chat_id=telegram_chat_id,
                )
            )

        return CanonicalPublicationLinkedForwardParityProof(
            publication_id=int(publication.id),
            source_channel_id=int(publication.channel_id),
            source_telegram_chat_id=int(plan.telegram_chat_id),
            forward_channel_ids=forward_ids,
            forward_targets=tuple(targets),
            disable_notification=bool(forward_silent),
        )
