from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


CANONICAL_EXECUTION_MODE = "canonical"
INTENTIONAL_LEGACY_EXECUTION_MODE = "intentional_legacy"
PUBLICATION_EXECUTION_MODES = frozenset(
    {CANONICAL_EXECUTION_MODE, INTENTIONAL_LEGACY_EXECUTION_MODE}
)

CANONICAL_SCHEDULING_OUTCOME = "canonical"
LEGACY_ALLOWLISTED_SCHEDULING_OUTCOME = "legacy_allowlisted"
UNSUPPORTED_REJECT_SCHEDULING_OUTCOME = "unsupported_reject"
SCHEDULING_BOUNDARY_OUTCOMES = frozenset(
    {
        CANONICAL_SCHEDULING_OUTCOME,
        LEGACY_ALLOWLISTED_SCHEDULING_OUTCOME,
        UNSUPPORTED_REJECT_SCHEDULING_OUTCOME,
    }
)

_RUNTIME_OPTION_KEYS = frozenset(
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
class SchedulingBoundaryDecision:
    """Queue-time ownership decision for one immutable delivery intent."""

    outcome: str
    execution_mode: str | None
    runtime_options: dict[str, Any] | None
    reason: str


class UnsupportedSchedulingProfileError(ValueError):
    """Raised when fresh scheduling intent has no supported execution owner."""



def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return int(value)


def _normalize_runtime_options(
    value: Mapping[str, Any] | None,
    *,
    allow_unrelated_keys: bool,
) -> dict[str, Any] | None:
    if value is None:
        source: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        source = {str(key): item for key, item in value.items()}
    else:
        return None

    if not allow_unrelated_keys and not set(source).issubset(_RUNTIME_OPTION_KEYS):
        return None

    options: dict[str, Any] = {}

    if "silent" in source:
        silent = source.get("silent")
        if type(silent) is not bool:
            return None
        options["silent"] = silent

    if "pin_on" in source:
        pin_on = source.get("pin_on")
        if type(pin_on) is not bool:
            return None
        if pin_on:
            options["pin_on"] = True

    if "forward_to" in source:
        raw_forward = source.get("forward_to")
        if raw_forward not in (None, []):
            if not isinstance(raw_forward, list):
                return None
            normalized_forward: list[int] = []
            for raw_channel_id in raw_forward:
                if isinstance(raw_channel_id, bool) or not isinstance(raw_channel_id, int):
                    return None
                if raw_channel_id <= 0:
                    return None
                normalized_forward.append(int(raw_channel_id))
            if not normalized_forward:
                return None
            options["forward_to"] = normalized_forward

    for key in ("autodelete_seconds", "autodelete_views"):
        if key not in source:
            continue
        raw_value = source.get(key)
        if raw_value in (None, 0):
            continue
        parsed = _positive_int(raw_value)
        if parsed is None:
            return None
        options[key] = parsed

    if "autodelete_report" in source:
        report = source.get("autodelete_report")
        if type(report) is not bool:
            return None
        if report:
            options["autodelete_report"] = True

    return options


def _fresh_mode_from_normalized_options(options: Mapping[str, Any]) -> str | None:
    has_time = "autodelete_seconds" in options
    has_views = "autodelete_views" in options
    report = options.get("autodelete_report") is True

    # #509 owns migration of the one retained legacy profile. Keep the allowlist
    # intentionally exact and independent of any current worker-readiness facts.
    if has_time and has_views:
        return INTENTIONAL_LEGACY_EXECUTION_MODE

    # Report is a canonical side effect only when attached to one supported delete
    # trigger. Report-only intent has no delete owner and therefore has no owner at all.
    if report and not (has_time or has_views):
        return None

    # Every other normalized fresh profile is in the canonical ownership lattice:
    # plain, pin, forward, time*, views*, and their supported report compositions.
    return CANONICAL_EXECUTION_MODE


def _historical_mode_from_normalized_options(options: Mapping[str, Any]) -> str | None:
    """Preserve ownership encoded by legacy-shaped rows created before #508."""

    has_time = "autodelete_seconds" in options
    has_views = "autodelete_views" in options
    report = options.get("autodelete_report") is True

    if has_time and has_views:
        return INTENTIONAL_LEGACY_EXECUTION_MODE
    if report:
        if has_time or has_views:
            return INTENTIONAL_LEGACY_EXECUTION_MODE
        return None
    return CANONICAL_EXECUTION_MODE


def _boundary_from_normalized_options(
    options: dict[str, Any] | None,
) -> SchedulingBoundaryDecision:
    if options is None:
        return SchedulingBoundaryDecision(
            outcome=UNSUPPORTED_REJECT_SCHEDULING_OUTCOME,
            execution_mode=None,
            runtime_options=None,
            reason="invalid_runtime_options",
        )

    mode = _fresh_mode_from_normalized_options(options)
    if mode == CANONICAL_EXECUTION_MODE:
        return SchedulingBoundaryDecision(
            outcome=CANONICAL_SCHEDULING_OUTCOME,
            execution_mode=CANONICAL_EXECUTION_MODE,
            runtime_options=dict(options),
            reason="supported_canonical_profile",
        )
    if mode == INTENTIONAL_LEGACY_EXECUTION_MODE:
        return SchedulingBoundaryDecision(
            outcome=LEGACY_ALLOWLISTED_SCHEDULING_OUTCOME,
            execution_mode=INTENTIONAL_LEGACY_EXECUTION_MODE,
            runtime_options=dict(options),
            reason="retained_legacy_mixed_time_views",
        )
    return SchedulingBoundaryDecision(
        outcome=UNSUPPORTED_REJECT_SCHEDULING_OUTCOME,
        execution_mode=None,
        runtime_options=dict(options),
        reason="unsupported_runtime_profile",
    )


def runtime_options_from_legacy_payload(
    payload: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalize immutable runtime intent carried by a legacy-shaped queue payload."""

    if payload is None:
        source: dict[str, Any] = {}
    elif isinstance(payload, Mapping):
        source = {str(key): item for key, item in payload.items()}
    else:
        return None
    return _normalize_runtime_options(source, allow_unrelated_keys=True)


def scheduling_boundary_from_runtime_options(
    runtime_options: Mapping[str, Any] | None,
) -> SchedulingBoundaryDecision:
    """Return the explicit fresh-ingress owner for one runtime-options mapping."""

    return _boundary_from_normalized_options(
        _normalize_runtime_options(runtime_options, allow_unrelated_keys=False)
    )


def scheduling_boundary_from_legacy_payload(
    payload: Mapping[str, Any] | None,
) -> SchedulingBoundaryDecision:
    """Return the explicit fresh-ingress owner for one legacy-shaped source payload."""

    if payload is None:
        source: dict[str, Any] = {}
    elif isinstance(payload, Mapping):
        source = {str(key): item for key, item in payload.items()}
    else:
        return _boundary_from_normalized_options(None)

    if "repeat_on" in source:
        repeat_on = source.get("repeat_on")
        if type(repeat_on) is not bool:
            return _boundary_from_normalized_options(None)
        if repeat_on:
            repeat_seconds = _positive_int(source.get("repeat_seconds"))
            if repeat_seconds is None:
                return _boundary_from_normalized_options(None)

    return _boundary_from_normalized_options(
        _normalize_runtime_options(source, allow_unrelated_keys=True)
    )


def execution_mode_from_runtime_options(
    runtime_options: Mapping[str, Any] | None,
) -> str | None:
    """Classify fresh immutable queue-time runtime intent without live readiness facts."""

    return scheduling_boundary_from_runtime_options(runtime_options).execution_mode


def execution_mode_from_legacy_payload(
    payload: Mapping[str, Any] | None,
) -> str | None:
    """Classify one historical legacy queue occurrence from source intent only.

    This intentionally preserves pre-#508 report ownership for already-created linked
    PostTask rows. Fresh scheduling must use `scheduling_boundary_from_legacy_payload`
    instead, so report-capable profiles cannot silently fall back to legacy transport.
    """

    if payload is None:
        source: dict[str, Any] = {}
    elif isinstance(payload, Mapping):
        source = {str(key): item for key, item in payload.items()}
    else:
        return None

    if "repeat_on" in source:
        repeat_on = source.get("repeat_on")
        if type(repeat_on) is not bool:
            return None
        if repeat_on:
            repeat_seconds = _positive_int(source.get("repeat_seconds"))
            if repeat_seconds is None:
                return None

    options = runtime_options_from_legacy_payload(source)
    if options is None:
        return None
    return _historical_mode_from_normalized_options(options)


def has_canonical_execution_authority(execution_mode: object) -> bool:
    """Return whether persisted execution mode explicitly grants canonical authority."""

    return execution_mode == CANONICAL_EXECUTION_MODE
