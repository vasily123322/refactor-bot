from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


AI_RUN_LEASE_SECONDS = 15 * 60
LEASE_EXPIRED_ERROR = "LeaseExpired"
LEASE_EXPIRED_REASON = "lease_expired"


def utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ai_run_lease_expired(
    started_at: datetime | None,
    *,
    now: datetime | None = None,
    lease_seconds: int = AI_RUN_LEASE_SECONDS,
) -> bool:
    started = utc_datetime(started_at)
    # A persisted running row without a start timestamp cannot prove it still owns
    # a lease. Fail toward recovery rather than blocking a candidate forever.
    if started is None:
        return True
    current = utc_datetime(now) or datetime.now(timezone.utc)
    return current - started >= timedelta(seconds=max(1, int(lease_seconds)))


def abandon_expired_ai_run(
    run: Any,
    *,
    now: datetime | None = None,
) -> None:
    current = utc_datetime(now) or datetime.now(timezone.utc)
    run.status = "abandoned"
    run.error = LEASE_EXPIRED_ERROR
    run.finished_at = current
    run.output = {
        **dict(getattr(run, "output", None) or {}),
        "recovery_reason": LEASE_EXPIRED_REASON,
    }
