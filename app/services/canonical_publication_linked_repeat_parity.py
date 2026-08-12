from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlan
from app.services.canonical_publication_legacy_transport_handoff import (
    _FORBIDDEN_EPHEMERAL_KEYS,
    _IDENTITY_MARKERS,
    _expected_transport_payload,
    _identity_markers_match,
    _mapping,
    _silent_intent_matches,
    _strip_neutral_effect_fields,
    _supported_runtime_options,
)
from app.services.scheduling import as_utc


_NEUTRAL_NUMBER_VALUES = (None, False, 0, "0", "")


@dataclass(frozen=True, slots=True)
class CanonicalPublicationLinkedRepeatParityProof:
    publication_id: int
    legacy_post_task_id: int
    repeat_group_id: int
    repeat_seconds: int
    root_occurrence: bool


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
    # Unknown future repeat semantics must not be silently absorbed into the fixed-delay
    # authority profile.
    if not set(rule).issubset({"enabled", "seconds"}):
        return None
    return seconds, deepcopy(rule)


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


class CanonicalPublicationLinkedRepeatParityService:
    """Read-only proof for pristine fixed-delay linked repeat handoff.

    This first repeat profile intentionally admits only the already-established
    empty/explicit-silent runtime slice. Pin/forward/time/views composition can be widened
    separately after fixed-delay lineage and successor recovery are proven in isolation.

    Root bridge occurrences do not carry `repeat_group_id` in PostTask payload: the group
    becomes known only after the root PostTask is flushed and is stored in canonical
    Publication/Schedule metadata. Such absence is accepted only when canonical group id
    equals the exact linked root PostTask id. Successor occurrences must carry an explicit
    payload group id equal to canonical metadata.
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

        runtime_options = _supported_runtime_options(plan)
        current = _mapping(task.payload)
        expected = _expected_transport_payload(plan)
        if runtime_options is None or current is None or expected is None:
            return None
        if any(key in current for key in _FORBIDDEN_EPHEMERAL_KEYS):
            return None
        if not _identity_markers_match(current, publication=publication, plan=plan):
            return None
        if not _silent_intent_matches(current, expected, runtime_options):
            return None
        if not _generated_state_is_pristine(current):
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

        # Remove only the repeat fields whose exact parity was proven above, then reuse
        # the established immutable transport comparison. Any hidden pin/forward/delete
        # or unknown effect remains visible and causes fail-closed rejection.
        current_clean = deepcopy(current)
        for key in _IDENTITY_MARKERS:
            current_clean.pop(key, None)
        for key in ("repeat_on", "repeat_seconds", "repeat_group_id"):
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
        )
