from __future__ import annotations

import pytest

from app.services.publication_autodelete import _is_unavailable_delete_error


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: message to delete not found",
        "Bad Request: MESSAGE_ID_INVALID",
        "Bad Request: message can't be deleted",
        "Bad Request: message cannot be deleted",
    ],
)
def test_deterministic_unavailable_provider_errors_are_terminal_evidence(message) -> None:
    # This classifier is inherited from the mature time-autodelete runtime. The new
    # destructive ledger records these outcomes as `unavailable` terminal evidence for
    # the exact reserved chat/message; this PR intentionally does not broaden the set.
    assert _is_unavailable_delete_error(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "network timeout",
        "connection reset by peer",
        "temporary provider failure",
        "internal server error",
    ],
)
def test_generic_or_transport_provider_errors_are_not_terminal_unavailable(message) -> None:
    # Generic uncertainty must flow to the durable `unknown` no-replay state instead of
    # falsely claiming terminal deletion/unavailability.
    assert not _is_unavailable_delete_error(RuntimeError(message))
