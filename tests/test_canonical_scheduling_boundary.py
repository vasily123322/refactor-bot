from __future__ import annotations

import pytest

from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    CANONICAL_SCHEDULING_OUTCOME,
    INTENTIONAL_LEGACY_EXECUTION_MODE,
    LEGACY_ALLOWLISTED_SCHEDULING_OUTCOME,
    UNSUPPORTED_REJECT_SCHEDULING_OUTCOME,
    execution_mode_from_legacy_payload,
    execution_mode_from_runtime_options,
    scheduling_boundary_from_legacy_payload,
    scheduling_boundary_from_runtime_options,
)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"autodelete_seconds": 60},
        {"autodelete_views": 10},
        {"autodelete_seconds": 60, "autodelete_report": True},
        {"autodelete_views": 10, "autodelete_report": True},
        {
            "pin_on": True,
            "autodelete_seconds": 60,
            "autodelete_report": True,
        },
        {
            "forward_to": [123],
            "autodelete_views": 10,
            "autodelete_report": True,
        },
    ],
)
def test_supported_fresh_profiles_are_canonical(options):
    decision = scheduling_boundary_from_runtime_options(options)

    assert decision.outcome == CANONICAL_SCHEDULING_OUTCOME
    assert decision.execution_mode == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_runtime_options(options) == CANONICAL_EXECUTION_MODE


def test_mixed_time_and_views_is_retained_fresh_legacy():
    options = {
        "autodelete_seconds": 60,
        "autodelete_views": 10,
        "autodelete_report": True,
    }

    decision = scheduling_boundary_from_runtime_options(options)

    assert decision.outcome == LEGACY_ALLOWLISTED_SCHEDULING_OUTCOME
    assert decision.execution_mode == INTENTIONAL_LEGACY_EXECUTION_MODE
    assert (
        execution_mode_from_runtime_options(options)
        == INTENTIONAL_LEGACY_EXECUTION_MODE
    )


@pytest.mark.parametrize(
    "options",
    [
        {"autodelete_report": True},
        {"autodelete_seconds": -1},
        {"autodelete_views": "10"},
        {"pin_on": 1},
        {"forward_to": [0]},
        {"unknown": True},
    ],
)
def test_unsupported_fresh_profiles_have_explicit_reject_outcome(options):
    decision = scheduling_boundary_from_runtime_options(options)

    assert decision.outcome == UNSUPPORTED_REJECT_SCHEDULING_OUTCOME
    assert decision.execution_mode is None
    # Classification remains side-effect free for internal proof/planner callers.
    # Fresh queue entrypoints consume the explicit boundary and reject before writes.
    assert execution_mode_from_runtime_options(options) is None


def test_historical_report_profile_keeps_legacy_ownership():
    payload = {
        "autodelete_seconds": 60,
        "autodelete_report": True,
    }

    assert (
        execution_mode_from_legacy_payload(payload)
        == INTENTIONAL_LEGACY_EXECUTION_MODE
    )


def test_historical_mixed_profile_keeps_legacy_ownership():
    payload = {
        "autodelete_seconds": 60,
        "autodelete_views": 10,
    }

    assert (
        execution_mode_from_legacy_payload(payload)
        == INTENTIONAL_LEGACY_EXECUTION_MODE
    )


def test_repeat_metadata_preserves_supported_fresh_ownership():
    payload = {
        "autodelete_seconds": 60,
        "autodelete_report": True,
        "repeat_on": True,
        "repeat_seconds": 300,
    }

    decision = scheduling_boundary_from_legacy_payload(payload)

    assert decision.outcome == CANONICAL_SCHEDULING_OUTCOME
    assert decision.execution_mode == CANONICAL_EXECUTION_MODE


@pytest.mark.parametrize(
    "payload",
    [
        {"repeat_on": "yes"},
        {"repeat_on": True, "repeat_seconds": 0},
        {"repeat_on": True, "repeat_seconds": "300"},
    ],
)
def test_invalid_repeat_metadata_is_rejected(payload):
    decision = scheduling_boundary_from_legacy_payload(payload)

    assert decision.outcome == UNSUPPORTED_REJECT_SCHEDULING_OUTCOME
    assert decision.execution_mode is None
