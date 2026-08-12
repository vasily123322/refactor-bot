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


def test_runtime_capability_rejects_unknown_types_duplicates_and_unbounded_fanout() -> None:
    invalid = [
        {"autodelete_seconds": 10},
        {"silent": 1},
        {"pin_on": 1},
        {"forward_to": None},
        {"forward_to": [1, 1]},
        {"forward_to": [True]},
        {"forward_to": [0]},
        {"forward_to": [-1]},
        {"forward_to": list(range(1, 102))},
    ]
    for options in invalid:
        assert parse_canonical_publication_delivery_runtime_capability(options) is None
