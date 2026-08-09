from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.sources.models import SourceConnector


WORKER_FAILURE_COUNT_KEY = "worker_failure_count"
WORKER_RETRY_AFTER_KEY = "worker_retry_after"
WORKER_FAILURE_KIND_KEY = "worker_failure_kind"

BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def source_worker_failure_count(connector: SourceConnector) -> int:
    raw = dict(connector.config or {}).get(WORKER_FAILURE_COUNT_KEY)
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def source_worker_retry_after(connector: SourceConnector) -> datetime | None:
    raw = dict(connector.config or {}).get(WORKER_RETRY_AFTER_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def source_worker_failure_kind(connector: SourceConnector) -> str | None:
    raw = dict(connector.config or {}).get(WORKER_FAILURE_KIND_KEY)
    value = str(raw or "").strip()
    return value or None


def source_worker_backoff_active(
    connector: SourceConnector,
    *,
    now: datetime | None = None,
) -> bool:
    retry_after = source_worker_retry_after(connector)
    return retry_after is not None and retry_after > _utc(now)


def _backoff_seconds(failure_count: int) -> int:
    exponent = max(0, min(int(failure_count) - 1, 16))
    return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2**exponent))


def mark_source_worker_failure(
    connector: SourceConnector,
    *,
    failure_kind: str,
    now: datetime | None = None,
) -> datetime:
    current = _utc(now)
    failure_count = source_worker_failure_count(connector) + 1
    retry_after = current + timedelta(seconds=_backoff_seconds(failure_count))
    connector.config = {
        **dict(connector.config or {}),
        WORKER_FAILURE_COUNT_KEY: failure_count,
        WORKER_RETRY_AFTER_KEY: retry_after.isoformat(),
        WORKER_FAILURE_KIND_KEY: str(failure_kind).strip()[:32] or "failure",
    }
    return retry_after


def clear_source_worker_failure(connector: SourceConnector) -> bool:
    current = dict(connector.config or {})
    changed = False
    for key in (
        WORKER_FAILURE_COUNT_KEY,
        WORKER_RETRY_AFTER_KEY,
        WORKER_FAILURE_KIND_KEY,
    ):
        if key in current:
            current.pop(key, None)
            changed = True
    if changed:
        connector.config = current
    return changed
