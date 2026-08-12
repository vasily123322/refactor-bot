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
_REPEAT_RUNTIME_KEYS = frozenset(
    {
        "silent",
        "pin_on",
        "forward_to",
        "autodelete_seconds",
        "autodelete_views",
        "autodelete_report",
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationLinkedRepeatParityProof:
    publication_id: int
    legacy_post_task_id: int
    repeat_group_id: int
    repeat_seconds: int
    root_occurrence: bool
    pin_on: bool = False
    forward_channel_ids: tuple[int, ...] = ()
    time_autodelete_seconds: int | None = None
    views_autodelete_threshold: int | None = None
    autodelete_report: bool = False
    views_pin_forward_composed: bool = False


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


def _repeat_runtime_options(
    plan: CanonicalPublicationDeliveryPlan,
) -> tuple[dict[str, Any], tuple[int, ...], int | None, int | None, bool] | None:
    try:
        options = plan.runtime_options()
    except (TypeError, ValueError):
        return None
    if not isinstance(options, dict) or not set(options).issubset(_REPEAT_RUNTIME_KEYS):
        return None
    if "silent" in options and type(options.get("silent")) is not bool:
        return None
    if "pin_on" in options and type(options.get("pin_on")) is not bool:
        return None

    capability = parse_canonical_publication_delivery_runtime_capability(options)
    if capability is None:
        return None
    forward_ids = tuple(int(channel_id) for channel_id in capability.forward_to)
    time_seconds = capability.time_autodelete_seconds
    views_threshold = capability.views_autodelete_threshold
    report = bool(capability.autodelete_report)

    if time_seconds is not None:
        # First time-family stage is plain/silent repeat+time only. Pin/forward each need
        # later independent composition proofs, and the generic parser keeps time+views
        # mutually exclusive. Effective/generated timer keys are not admitted at all.
        if "autodelete_seconds" not in options or _positive_int(
            options.get("autodelete_seconds")
        ) != int(time_seconds):
            return None
        if capability.pin_on or forward_ids:
            return None
    elif views_threshold is None and "autodelete_report" in options:
        # Report has no independent meaning. This stays explicit against future parser
        # widening even though the generic parser already rejects it.
        return None

    return deepcopy(options), forward_ids, time_seconds, views_threshold, report


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


def _legacy_views_intent_matches(
    payload: Mapping[str, Any],
    runtime_options: Mapping[str, Any],
    *,
    threshold: int | None,
    report: bool,
) -> bool:
    if threshold is None:
        return True

    if (
        "autodelete_views" not in payload
        or payload.get("autodelete_views") != runtime_options.get("autodelete_views")
        or _positive_int(payload.get("autodelete_views")) != threshold
    ):
        return False

    if "autodelete_report" in runtime_options:
        if (
            type(payload.get("autodelete_report")) is not bool
            or payload.get("autodelete_report") is not report
        ):
            return False
    elif payload.get("autodelete_report") not in (None, False):
        return False

    if payload.get("autodelete_seconds") not in _NEUTRAL_NUMBER_VALUES:
        return False
    return True


def _legacy_time_intent_matches(
    payload: Mapping[str, Any],
    runtime_options: Mapping[str, Any],
    *,
    seconds: int | None,
    report: bool,
) -> bool:
    if seconds is None:
        return True

    if (
        "autodelete_seconds" not in payload
        or payload.get("autodelete_seconds") != runtime_options.get("autodelete_seconds")
        or _positive_int(payload.get("autodelete_seconds")) != int(seconds)
    ):
        return False

    if "autodelete_report" in runtime_options:
        if (
            type(payload.get("autodelete_report")) is not bool
            or payload.get("autodelete_report") is not report
        ):
            return False
    elif payload.get("autodelete_report") not in (None, False):
        return False

    # Time and views stay mutually exclusive at parity as well as in the generic parser.
    if payload.get("autodelete_views") not in _NEUTRAL_NUMBER_VALUES:
        return False
    return True


class CanonicalPublicationLinkedRepeatParityService:
    """Read-only proof for pristine fixed-delay linked repeat handoff.

    Existing pin/forward/views slices retain their exact parity. Plain/silent repeat+time
    now has a read-only parity slice for queue-time `autodelete_seconds` and optional
    report intent, while pin/forward time compositions remain closed for later stages.

    This is evidence only. #330 explicitly hard-closes every repeat+time strict claim until
    the current ancestry converges with the durable per-message #281 destructive ledger.
    No primary, timer materialization or Telegram DELETE authority is created here.
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

        runtime_profile = _repeat_runtime_options(plan)
        current = _mapping(task.payload)
        expected = _expected_transport_payload(plan)
        if runtime_profile is None or current is None or expected is None:
            return None
        (
            runtime_options,
            forward_ids,
            time_seconds,
            views_threshold,
            autodelete_report,
        ) = runtime_profile
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
        if not _legacy_views_intent_matches(
            current,
            runtime_options,
            threshold=views_threshold,
            report=autodelete_report,
        ):
            return None
        if not _legacy_time_intent_matches(
            current,
            runtime_options,
            seconds=time_seconds,
            report=autodelete_report,
        ):
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
        if views_threshold is not None:
            current_clean.pop("autodelete_views", None)
            current_clean.pop("autodelete_report", None)
        if time_seconds is not None:
            current_clean.pop("autodelete_seconds", None)
            current_clean.pop("autodelete_report", None)
        stripped_current = _strip_neutral_effect_fields(current_clean)
        stripped_expected = _strip_neutral_effect_fields(deepcopy(expected))
        if (
            stripped_current is None
            or stripped_expected is None
            or stripped_current != stripped_expected
        ):
            return None

        combined = bool(views_threshold is not None and pin_on and forward_ids)
        return CanonicalPublicationLinkedRepeatParityProof(
            publication_id=publication_id,
            legacy_post_task_id=task_id,
            repeat_group_id=group_id,
            repeat_seconds=repeat_seconds,
            root_occurrence=root_occurrence,
            pin_on=pin_on,
            forward_channel_ids=forward_ids,
            time_autodelete_seconds=(
                int(time_seconds) if time_seconds is not None else None
            ),
            views_autodelete_threshold=views_threshold,
            autodelete_report=autodelete_report,
            views_pin_forward_composed=combined,
        )
