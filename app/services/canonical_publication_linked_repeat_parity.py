from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlan
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    _FORBIDDEN_EPHEMERAL_KEYS,
    _IDENTITY_MARKERS,
    _expected_transport_payload,
    _identity_markers_match,
    _mapping,
    _silent_intent_matches,
    _strip_neutral_effect_fields,
)
from app.services.scheduling import as_utc


_NEUTRAL_NUMBER_VALUES = (None, False, 0, "0", "")
_REPEAT_FORWARD_RUNTIME_KEYS = frozenset({"silent", "pin_on", "forward_to"})


@dataclass(frozen=True, slots=True)
class CanonicalPublicationLinkedRepeatParityProof:
    publication_id: int
    legacy_post_task_id: int
    repeat_group_id: int
    repeat_seconds: int
    root_occurrence: bool
    pin_on: bool = False
    forward_channel_ids: tuple[int, ...] = ()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _repeat_rule(plan: CanonicalPublicationDeliveryPlan) -> tuple[int, dict[str, Any]] | None:
    try:
        rule = plan.repeat_rule()
    except (TypeError, ValueError):
        return None
    if not isinstance(rule, dict) or rule.get("enabled") is not True:
        return None
    seconds = _positive_int(rule.get("seconds"))
    if seconds is None:
        return None
    if not set(rule).issubset({"enabled", "seconds"}):
        return None
    return seconds, deepcopy(rule)


def _repeat_forward_runtime_options(
    plan: CanonicalPublicationDeliveryPlan,
) -> tuple[dict[str, Any], tuple[int, ...]] | None:
    try:
        options = plan.runtime_options()
    except (TypeError, ValueError):
        return None
    if not isinstance(options, dict) or not set(options).issubset(
        _REPEAT_FORWARD_RUNTIME_KEYS
    ):
        return None
    if "silent" in options and type(options.get("silent")) is not bool:
        return None
    if "pin_on" in options and type(options.get("pin_on")) is not bool:
        return None

    capability = parse_canonical_publication_delivery_runtime_capability(options)
    if capability is None:
        return None
    forward_ids = tuple(int(channel_id) for channel_id in capability.forward_to)

    # repeat+pin and repeat+forward are intentionally separate migration slices. The
    # composition remains fail-closed until its own authority/replay proof.
    if bool(capability.pin_on) and forward_ids:
        return None
    return deepcopy(options), forward_ids


def _canonical_group_id(
    publication: Publication,
    schedule: ScheduleEntry,
) -> int | None:
    if not isinstance(publication.meta, Mapping) or not isinstance(schedule.meta, Mapping):
        return None
    publication_group = _positive_int(dict(publication.meta).get("repeat_group_id"))
    schedule_group = _positive_int(dict(schedule.meta).get("repeat_group_id"))
    if publication_group is None or schedule_group is None:
        return None
    return publication_group if publication_group == schedule_group else None


def _generated_state_is_pristine(payload: Mapping[str, Any]) -> bool:
    if payload.get("result_ids") not in (None, []):
        return False
    if payload.get("result_link") not in (None, ""):
        return False
    if payload.get("autodelete_effective_seconds") not in _NEUTRAL_NUMBER_VALUES:
        return False
    if payload.get("autodelete_at") not in (None, ""):
        return False
    if payload.get("autodeleted") not in (None, False):
        return False
    if payload.get("autodeleted_at") not in (None, ""):
        return False
    return True


def _legacy_forward_ids(payload: Mapping[str, Any]) -> tuple[int, ...] | None:
    raw = payload.get("forward_to")
    if raw in (None, False):
        return ()
    if not isinstance(raw, list) or len(raw) > 100:
        return None
    normalized: list[int] = []
    seen: set[int] = set()
    for value in raw:
        parsed = _positive_int(value)
        if parsed is None or parsed in seen:
            return None
        seen.add(parsed)
        normalized.append(parsed)
    return tuple(normalized)


class CanonicalPublicationLinkedRepeatParityService:
    """Read-only proof for pristine fixed-delay linked repeat handoff.

    Proven repeat effects are intentionally independent slices: optional pin OR ordered
    forward intent, plus optional explicit silent. Forward target IDs are exact ordered
    internal Channel IDs, matching the established non-repeat forward parity contract.
    Pin+forward and both delete modes remain outside this proof.

    This is still parity only. The strict repeat capability claim remains the independent
    authority barrier until a later PR deliberately widens it to forward.
    """

    def prove(
        self,
        *,
        task: PostTask,
        publication: Publication,
        schedule: ScheduleEntry,
        plan: CanonicalPublicationDeliveryPlan,
    ) -> CanonicalPublicationLinkedRepeatParityProof | None:
        if publication.legacy_post_task_id is None:
            return None
        try:
            task_id = int(task.id)
            publication_id = int(publication.id)
            linked_task_id = int(publication.legacy_post_task_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if task_id <= 0 or publication_id <= 0 or task_id != linked_task_id:
            return None
        if int(task.channel_id) != int(plan.channel_id):
            return None
        if task.scheduled_at is None or as_utc(task.scheduled_at) != as_utc(plan.scheduled_at):
            return None
        if task.error not in (None, ""):
            return None

        repeat = _repeat_rule(plan)
        if repeat is None:
            return None
        repeat_seconds, _ = repeat
        try:
            schedule_rule = schedule.repeat_rule
        except Exception:
            return None
        if not isinstance(schedule_rule, Mapping):
            return None
        if dict(schedule_rule) != {"enabled": True, "seconds": repeat_seconds}:
            return None

        runtime_profile = _repeat_forward_runtime_options(plan)
        current = _mapping(task.payload)
        expected = _expected_transport_payload(plan)
        if runtime_profile is None or current is None or expected is None:
            return None
        runtime_options, forward_ids = runtime_profile
        if any(key in current for key in _FORBIDDEN_EPHEMERAL_KEYS):
            return None
        if not _identity_markers_match(current, publication=publication, plan=plan):
            return None
        if not _silent_intent_matches(current, expected, runtime_options):
            return None
        if not _generated_state_is_pristine(current):
            return None

        pin_on = bool(runtime_options.get("pin_on", False))
        if pin_on:
            if type(current.get("pin_on")) is not bool or current.get("pin_on") is not True:
                return None
        elif current.get("pin_on") not in (None, False, 0):
            return None

        legacy_forward_ids = _legacy_forward_ids(current)
        if legacy_forward_ids is None or legacy_forward_ids != forward_ids:
            return None

        if current.get("repeat_on") is not True:
            return None
        if _positive_int(current.get("repeat_seconds")) != repeat_seconds:
            return None

        group_id = _canonical_group_id(publication, schedule)
        if group_id is None:
            return None
        raw_task_group = current.get("repeat_group_id")
        root_occurrence = raw_task_group is None
        if root_occurrence:
            if group_id != task_id:
                return None
        else:
            task_group = _positive_int(raw_task_group)
            if task_group is None or task_group != group_id:
                return None

        # Remove only effects whose exact parity was proven above. Hidden delete or
        # unknown effects remain visible and fail the established immutable comparison.
        current_clean = deepcopy(current)
        for key in _IDENTITY_MARKERS:
            current_clean.pop(key, None)
        for key in (
            "repeat_on",
            "repeat_seconds",
            "repeat_group_id",
            "pin_on",
            "forward_to",
        ):
            current_clean.pop(key, None)
        stripped_current = _strip_neutral_effect_fields(current_clean)
        stripped_expected = _strip_neutral_effect_fields(deepcopy(expected))
        if (
            stripped_current is None
            or stripped_expected is None
            or stripped_current != stripped_expected
        ):
            return None

        return CanonicalPublicationLinkedRepeatParityProof(
            publication_id=publication_id,
            legacy_post_task_id=task_id,
            repeat_group_id=group_id,
            repeat_seconds=repeat_seconds,
            root_occurrence=root_occurrence,
            pin_on=pin_on,
            forward_channel_ids=forward_ids,
        )
