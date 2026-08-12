from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

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


_NEUTRAL_NUMBER_VALUES = (None, False, 0, "0", "")


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
    pin_on: bool = False
    time_autodelete_seconds: int | None = None
    views_autodelete_threshold: int | None = None
    autodelete_report: bool = False

    @property
    def time_autodelete_requested(self) -> bool:
        return self.time_autodelete_seconds is not None

    @property
    def views_autodelete_requested(self) -> bool:
        return self.views_autodelete_threshold is not None

    @property
    def delete_requested(self) -> bool:
        return self.time_autodelete_requested or self.views_autodelete_requested


def _strict_optional_positive_int(value: Any) -> tuple[bool, int | None]:
    if value in _NEUTRAL_NUMBER_VALUES:
        return True, None
    if isinstance(value, bool):
        return False, None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return False, None
    if parsed <= 0:
        return False, None
    return True, parsed


def _forward_profile(
    options: dict[str, Any],
) -> tuple[tuple[int, ...], bool, bool, int | None, int | None, bool] | None:
    capability = parse_canonical_publication_delivery_runtime_capability(options)
    if capability is None or not capability.forward_to:
        return None

    # Generated effective seconds are never valid bridge queue-time authority for a
    # linked occurrence. Time intent must come from exact base seconds only.
    if options.get("autodelete_effective_seconds") not in _NEUTRAL_NUMBER_VALUES:
        return None
    base_ok, base_seconds = _strict_optional_positive_int(
        options.get("autodelete_seconds")
    )
    views_ok, views_threshold = _strict_optional_positive_int(
        options.get("autodelete_views")
    )
    if not base_ok or not views_ok:
        return None
    if capability.time_autodelete_seconds != base_seconds:
        return None
    if capability.views_autodelete_threshold != views_threshold:
        return None

    report = options.get("autodelete_report", False)
    if type(report) is not bool:
        return None
    if report and base_seconds is None and views_threshold is None:
        return None

    return (
        capability.forward_to,
        capability.forward_silent,
        bool(capability.pin_on),
        base_seconds,
        views_threshold,
        bool(report),
    )


def _legacy_forward_intent_matches(
    *,
    task: PostTask,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
    forward_ids: tuple[int, ...],
    pin_on: bool,
    time_autodelete_seconds: int | None,
    views_autodelete_threshold: int | None,
    autodelete_report: bool,
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
    if not isinstance(raw_forward, list) or len(raw_forward) != len(forward_ids):
        return False
    normalized: list[int] = []
    for raw in raw_forward:
        if isinstance(raw, bool):
            return False
        try:
            normalized.append(int(raw))
        except (TypeError, ValueError, OverflowError):
            return False
    if tuple(normalized) != forward_ids:
        return False

    if pin_on:
        if type(current.get("pin_on")) is not bool or current.get("pin_on") is not True:
            return False
    elif current.get("pin_on") not in (None, False, 0):
        return False

    # Any execution/generated delete evidence means the legacy occurrence may already
    # have progressed and is no longer safe to transfer.
    if current.get("result_ids") not in (None, []):
        return False
    if current.get("result_link") not in (None, ""):
        return False
    if current.get("autodelete_at") not in (None, ""):
        return False
    if current.get("autodelete_effective_seconds") not in _NEUTRAL_NUMBER_VALUES:
        return False
    if current.get("autodeleted") not in (None, False):
        return False
    if current.get("autodeleted_at") not in (None, ""):
        return False

    if time_autodelete_seconds is not None:
        raw_seconds = runtime_options.get("autodelete_seconds")
        if (
            "autodelete_seconds" not in current
            or current.get("autodelete_seconds") != raw_seconds
        ):
            return False
        if "autodelete_views" in runtime_options:
            if current.get("autodelete_views") != runtime_options.get("autodelete_views"):
                return False
        elif current.get("autodelete_views") not in _NEUTRAL_NUMBER_VALUES:
            return False

    if views_autodelete_threshold is not None:
        raw_views = runtime_options.get("autodelete_views")
        if (
            "autodelete_views" not in current
            or current.get("autodelete_views") != raw_views
        ):
            return False
        if "autodelete_seconds" in runtime_options:
            if current.get("autodelete_seconds") != runtime_options.get(
                "autodelete_seconds"
            ):
                return False
        elif current.get("autodelete_seconds") not in _NEUTRAL_NUMBER_VALUES:
            return False

    if time_autodelete_seconds is None and views_autodelete_threshold is None:
        if current.get("autodelete_seconds") not in _NEUTRAL_NUMBER_VALUES:
            return False
        if current.get("autodelete_views") not in _NEUTRAL_NUMBER_VALUES:
            return False

    if "autodelete_report" in runtime_options:
        if (
            type(current.get("autodelete_report")) is not bool
            or current.get("autodelete_report") is not autodelete_report
        ):
            return False
    elif current.get("autodelete_report") not in (None, False):
        return False

    current_clean = deepcopy(current)
    for key in _IDENTITY_MARKERS:
        current_clean.pop(key, None)
    current_clean.pop("forward_to", None)
    if pin_on:
        current_clean.pop("pin_on", None)
    if time_autodelete_seconds is not None or views_autodelete_threshold is not None:
        for key in (
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
            current_clean.pop(key, None)

    if current_clean.get("repeat_on") not in (None, False, 0):
        return False

    stripped_current = _strip_neutral_effect_fields(current_clean)
    stripped_expected = _strip_neutral_effect_fields(deepcopy(expected))
    return bool(
        stripped_current is not None
        and stripped_expected is not None
        and stripped_current == stripped_expected
    )


class CanonicalPublicationLinkedForwardParityService:
    """Read-only parity for forward, optional pin, and one pristine delete trigger.

    Forward targets remain ordered internal Channel IDs. Exact pin parity is compatible
    because both runtimes pin the last primary message before forwarding. Time and views
    deletion are mutually exclusive and are accepted only from exact bridge queue-time
    base intent with no generated/result evidence. Executor availability is enforced by
    the later atomic coordinator, not this read-only proof.
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
        profile = _forward_profile(options)
        if profile is None:
            return None
        (
            forward_ids,
            forward_silent,
            pin_on,
            time_autodelete_seconds,
            views_autodelete_threshold,
            autodelete_report,
        ) = profile
        if not _legacy_forward_intent_matches(
            task=task,
            publication=publication,
            plan=plan,
            forward_ids=forward_ids,
            pin_on=pin_on,
            time_autodelete_seconds=time_autodelete_seconds,
            views_autodelete_threshold=views_autodelete_threshold,
            autodelete_report=autodelete_report,
        ):
            return None

        rows = (
            await self.session.execute(
                select(Channel).where(Channel.id.in_(list(forward_ids)))
            )
        ).scalars().all()
        by_id = {int(channel.id): channel for channel in rows}
        if len(by_id) != len(forward_ids):
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
            pin_on=pin_on,
            time_autodelete_seconds=time_autodelete_seconds,
            views_autodelete_threshold=views_autodelete_threshold,
            autodelete_report=autodelete_report,
        )
