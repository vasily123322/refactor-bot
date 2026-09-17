from __future__ import annotations

from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)


def test_runtime_capability_accepts_plain_silent_pin_and_ordered_forwards() -> None:
    plain = parse_canonical_publication_delivery_runtime_capability({})
    assert plain is not None
    assert plain.silent is None
    assert plain.pin_on is False
    assert plain.forward_to == ()
    assert plain.time_autodelete_requested is False
    assert plain.views_autodelete_requested is False

    full = parse_canonical_publication_delivery_runtime_capability(
        {
            "silent": True,
            "pin_on": True,
            "forward_to": [7, 3, 11],
        }
    )
    assert full is not None
    assert full.silent is True
    assert full.forward_silent is True
    assert full.pin_on is True
    assert full.forward_to == (7, 3, 11)

    explicit_false = parse_canonical_publication_delivery_runtime_capability(
        {"silent": False, "pin_on": False, "forward_to": []}
    )
    assert explicit_false is not None
    assert explicit_false.silent is False
    assert explicit_false.forward_silent is False


def test_runtime_capability_accepts_time_autodelete_with_legacy_effective_precedence() -> None:
    base = parse_canonical_publication_delivery_runtime_capability(
        {"autodelete_seconds": 90, "autodelete_report": True}
    )
    assert base is not None
    assert base.time_autodelete_seconds == 90
    assert base.time_autodelete_requested is True
    assert base.views_autodelete_requested is False
    assert base.autodelete_report is True

    effective = parse_canonical_publication_delivery_runtime_capability(
        {
            "silent": True,
            "pin_on": True,
            "forward_to": [7],
            "autodelete_seconds": 90,
            "autodelete_effective_seconds": 120,
            "autodelete_views": 0,
            "autodelete_report": False,
        }
    )
    assert effective is not None
    assert effective.time_autodelete_seconds == 120
    assert effective.views_autodelete_requested is False
    assert effective.autodelete_report is False

    neutral = parse_canonical_publication_delivery_runtime_capability(
        {
            "autodelete_seconds": 0,
            "autodelete_effective_seconds": "0",
            "autodelete_views": False,
            "autodelete_report": False,
        }
    )
    assert neutral is not None
    assert neutral.time_autodelete_requested is False
    assert neutral.views_autodelete_requested is False


def test_runtime_capability_accepts_views_autodelete_and_report_without_time_timer() -> None:
    views = parse_canonical_publication_delivery_runtime_capability(
        {
            "silent": True,
            "pin_on": True,
            "forward_to": [9],
            "autodelete_views": 250,
            "autodelete_report": True,
        }
    )
    assert views is not None
    assert views.views_autodelete_threshold == 250
    assert views.views_autodelete_requested is True
    assert views.time_autodelete_requested is False
    assert views.autodelete_report is True


def test_runtime_capability_accepts_mixed_time_views_with_shared_destructive_owner() -> None:
    mixed = parse_canonical_publication_delivery_runtime_capability(
        {
            "autodelete_seconds": 60,
            "autodelete_views": 10,
            "autodelete_report": True,
        }
    )
    assert mixed is not None
    assert mixed.time_autodelete_seconds == 60
    assert mixed.views_autodelete_threshold == 10
    assert mixed.time_autodelete_requested is True
    assert mixed.views_autodelete_requested is True
    assert mixed.autodelete_report is True

    effective = parse_canonical_publication_delivery_runtime_capability(
        {"autodelete_effective_seconds": 120, "autodelete_views": 20}
    )
    assert effective is not None
    assert effective.time_autodelete_seconds == 120
    assert effective.views_autodelete_threshold == 20


def test_runtime_capability_rejects_unknown_malformed_and_report_without_trigger() -> None:
    invalid = [
        {"future_side_effect": True},
        {"silent": 1},
        {"pin_on": 1},
        {"forward_to": None},
        {"forward_to": [1, 1]},
        {"forward_to": [True]},
        {"forward_to": [0]},
        {"forward_to": [-1]},
        {"forward_to": list(range(1, 102))},
        {"autodelete_seconds": "later"},
        {"autodelete_seconds": -1},
        {"autodelete_effective_seconds": -1},
        {"autodelete_views": "many"},
        {"autodelete_views": -1},
        {"autodelete_report": 1},
        {"autodelete_report": True},
    ]
    for options in invalid:
        assert parse_canonical_publication_delivery_runtime_capability(options) is None
