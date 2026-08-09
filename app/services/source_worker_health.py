from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class SourceWorkerTickStats:
    started_at: datetime
    finished_at: datetime
    window_selected: int = 0
    scheduled: int = 0
    processed: int = 0
    skipped_backoff: int = 0
    skipped_busy: int = 0
    lease_errors: int = 0
    failures: int = 0
    timeouts: int = 0
    ingestion_errors: int = 0
    unexpected_errors: int = 0
    new_documents: int = 0
    candidates_created: int = 0
    backlog_remaining: int = 0
    stopped_early: bool = False

    @property
    def duration_ms(self) -> int:
        delta = self.finished_at - self.started_at
        return max(0, int(delta.total_seconds() * 1000))


class SourceWorkerHealthRegistry:
    """In-process, low-cardinality operational summary for Sources v2 worker."""

    _COUNTER_FIELDS = (
        "window_selected",
        "scheduled",
        "processed",
        "skipped_backoff",
        "skipped_busy",
        "lease_errors",
        "failures",
        "timeouts",
        "ingestion_errors",
        "unexpected_errors",
        "new_documents",
        "candidates_created",
        "backlog_remaining",
    )

    def __init__(self) -> None:
        self.started_at = _utc_now()
        self.running = False
        self.ticks = 0
        self.last_tick: SourceWorkerTickStats | None = None
        self._totals = {field: 0 for field in self._COUNTER_FIELDS}

    def set_running(self, value: bool) -> None:
        self.running = bool(value)

    def record(self, stats: SourceWorkerTickStats) -> None:
        self.last_tick = stats
        self.ticks += 1
        for field in self._COUNTER_FIELDS:
            self._totals[field] += int(getattr(stats, field))

    def reset_for_tests(self) -> None:
        self.started_at = _utc_now()
        self.running = False
        self.ticks = 0
        self.last_tick = None
        self._totals = {field: 0 for field in self._COUNTER_FIELDS}

    def snapshot(self) -> dict[str, Any]:
        last: dict[str, Any] | None = None
        if self.last_tick is not None:
            last = asdict(self.last_tick)
            last["duration_ms"] = self.last_tick.duration_ms
        return {
            "running": self.running,
            "started_at": self.started_at,
            "ticks": self.ticks,
            "last_tick": last,
            "totals": dict(self._totals),
        }


source_worker_health = SourceWorkerHealthRegistry()
