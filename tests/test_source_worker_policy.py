from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.sources.models import SourceConnector
from app.services.source_worker_policy import (
    clear_source_worker_failure,
    mark_source_worker_failure,
    source_worker_backoff_active,
    source_worker_failure_count,
    source_worker_failure_kind,
    source_worker_retry_after,
)


def _connector() -> SourceConnector:
    return SourceConnector(
        channel_id=1,
        kind="url",
        value="https://example.com",
        config={"preserved": "value"},
    )


def test_worker_failure_backoff_is_exponential_bounded_and_clearable() -> None:
    connector = _connector()
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    first_retry = mark_source_worker_failure(
        connector,
        failure_kind="timeout",
        now=now,
    )
    assert first_retry == now + timedelta(seconds=60)
    assert source_worker_failure_count(connector) == 1
    assert source_worker_failure_kind(connector) == "timeout"
    assert source_worker_backoff_active(connector, now=now) is True

    second_retry = mark_source_worker_failure(
        connector,
        failure_kind="ingestion_error",
        now=now,
    )
    assert second_retry == now + timedelta(seconds=120)
    assert source_worker_failure_count(connector) == 2
    assert source_worker_failure_kind(connector) == "ingestion_error"
    assert source_worker_retry_after(connector) == second_retry

    for _ in range(20):
        capped = mark_source_worker_failure(
            connector,
            failure_kind="unexpected",
            now=now,
        )
    assert capped == now + timedelta(seconds=3600)

    assert clear_source_worker_failure(connector) is True
    assert source_worker_failure_count(connector) == 0
    assert source_worker_retry_after(connector) is None
    assert source_worker_failure_kind(connector) is None
    assert connector.config == {"preserved": "value"}
    assert clear_source_worker_failure(connector) is False


def test_worker_retry_parser_fails_open_for_malformed_internal_state() -> None:
    connector = _connector()
    connector.config = {
        "worker_failure_count": "not-a-number",
        "worker_retry_after": "not-a-date",
    }
    assert source_worker_failure_count(connector) == 0
    assert source_worker_retry_after(connector) is None
    assert source_worker_backoff_active(connector) is False
