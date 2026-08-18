from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CANONICAL_EXECUTION_MODE = "canonical"
INTENTIONAL_LEGACY_EXECUTION_MODE = "intentional_legacy"
PUBLICATION_EXECUTION_MODES = frozenset(
    {CANONICAL_EXECUTION_MODE, INTENTIONAL_LEGACY_EXECUTION_MODE}
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


def _mode_from_normalized_options(options: Mapping[str, Any]) -> str | None:
    has_time = "autodelete_seconds" in options
    has_views = "autodelete_views" in options
    report = options.get("autodelete_report") is True

    # The mixed time+views scheduler profile is an explicit retained legacy contract.
    # Report may also be present; time+views still wins the intentional fallback.
    if has_time and has_views:
        return INTENTIONAL_LEGACY_EXECUTION_MODE

    # Exact report fallback is supported only on an otherwise exact time/views profile.
    if report:
        if has_time or has_views:
            return INTENTIONAL_LEGACY_EXECUTION_MODE
        return None

    # Every other normalized profile is in the supported canonical lattice:
    # plain, pin, forward, pin+forward, time*, or views*.
    return CANONICAL_EXECUTION_MODE


def execution_mode_from_runtime_options(
    runtime_options: Mapping[str, Any] | None,
) -> str | None:
    """Classify immutable queue-time runtime intent without live readiness facts."""

    options = _normalize_runtime_options(
        runtime_options,
        allow_unrelated_keys=False,
    )
    if options is None:
        return None
    return _mode_from_normalized_options(options)


def execution_mode_from_legacy_payload(
    payload: Mapping[str, Any] | None,
) -> str | None:
    """Classify one legacy queue occurrence from source intent only.

    Content/render fields are intentionally ignored. The classification never reads a
    PostTask identity, Publication linkage, or current canonical worker-started state.
    Repeat shape is validated because malformed repeat intent must not gain canonical
    ownership merely by having an otherwise supported delivery profile.
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

    options = _normalize_runtime_options(source, allow_unrelated_keys=True)
    if options is None:
        return None
    return _mode_from_normalized_options(options)
