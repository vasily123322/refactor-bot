from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


DEFAULT_SOURCE_WORKER_HISTORY_SIZE = 20


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


def _serialize_tick(stats: SourceWorkerTickStats) -> dict[str, Any]:
    value = asdict(stats)
    value["duration_ms"] = stats.duration_ms
    return value


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

    def __init__(self, *, history_size: int = DEFAULT_SOURCE_WORKER_HISTORY_SIZE) -> None:
        self.history_size = max(1, min(int(history_size), 100))
        self.started_at = _utc_now()
        self.running = False
        self.ticks = 0
        self.last_tick: SourceWorkerTickStats | None = None
        self._history: deque[SourceWorkerTickStats] = deque(maxlen=self.history_size)
        self._totals = {field: 0 for field in self._COUNTER_FIELDS}

    def set_running(self, value: bool) -> None:
        self.running = bool(value)

    def record(self, stats: SourceWorkerTickStats) -> None:
        self.last_tick = stats
        self._history.append(stats)
        self.ticks += 1
        for field in self._COUNTER_FIELDS:
            self._totals[field] += int(getattr(stats, field))

    def reset_for_tests(self) -> None:
        self.started_at = _utc_now()
        self.running = False
        self.ticks = 0
        self.last_tick = None
        self._history.clear()
        self._totals = {field: 0 for field in self._COUNTER_FIELDS}

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "ticks": self.ticks,
            "history_size": self.history_size,
            "last_tick": (
                _serialize_tick(self.last_tick) if self.last_tick is not None else None
            ),
            "history": [_serialize_tick(stats) for stats in self._history],
            "totals": dict(self._totals),
        }


source_worker_health = SourceWorkerHealthRegistry()
