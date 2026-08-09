from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.source_worker_health import (
    SourceWorkerHealthRegistry,
    SourceWorkerTickStats,
)


def test_source_worker_health_history_is_bounded_ordered_and_keeps_lifetime_totals() -> None:
    registry = SourceWorkerHealthRegistry(history_size=3)
    base = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    for index in range(5):
        registry.record(
            SourceWorkerTickStats(
                started_at=base + timedelta(minutes=index),
                finished_at=base + timedelta(minutes=index, milliseconds=25),
                window_selected=index + 1,
                processed=index,
                new_documents=index * 2,
            )
        )

    snapshot = registry.snapshot()
    assert snapshot["ticks"] == 5
    assert snapshot["history_size"] == 3
    assert len(snapshot["history"]) == 3
    assert [row["processed"] for row in snapshot["history"]] == [2, 3, 4]
    assert snapshot["last_tick"]["processed"] == 4
    assert snapshot["last_tick"]["duration_ms"] == 25

    # Ring-buffer eviction affects only recent history, never process-lifetime totals.
    assert snapshot["totals"]["processed"] == 10
    assert snapshot["totals"]["new_documents"] == 20


def test_source_worker_health_history_size_is_safely_clamped_and_resettable() -> None:
    minimum = SourceWorkerHealthRegistry(history_size=0)
    maximum = SourceWorkerHealthRegistry(history_size=10_000)
    assert minimum.snapshot()["history_size"] == 1
    assert maximum.snapshot()["history_size"] == 100

    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    minimum.record(SourceWorkerTickStats(started_at=now, finished_at=now, processed=1))
    minimum.reset_for_tests()
    snapshot = minimum.snapshot()
    assert snapshot["ticks"] == 0
    assert snapshot["history"] == []
    assert snapshot["last_tick"] is None
    assert snapshot["totals"]["processed"] == 0
