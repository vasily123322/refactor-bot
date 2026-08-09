import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot import dispatcher
from app.workers.publication_scheduler import Scheduler as PublicationAwareScheduler
from app.workers.reliable_scheduler import Scheduler as ReliableScheduler
from app.workers.scheduler import Scheduler as BaseScheduler


def _scheduler() -> ReliableScheduler:
    posting = SimpleNamespace(bot=SimpleNamespace())
    return ReliableScheduler(lambda: None, posting, interval_seconds=1)


def test_dispatcher_uses_publication_aware_reliability_scheduler() -> None:
    assert dispatcher.Scheduler is PublicationAwareScheduler
    assert issubclass(PublicationAwareScheduler, ReliableScheduler)
    assert issubclass(ReliableScheduler, BaseScheduler)


def test_overdue_repeat_commit_failure_rolls_back_and_propagates() -> None:
    scheduler = _scheduler()
    scheduler._boot_time = datetime.now(timezone.utc)
    post = SimpleNamespace(
        id=10,
        channel_id=20,
        status="pending",
        scheduled_at=scheduler._boot_time - timedelta(minutes=5),
    )
    session = SimpleNamespace(
        add=Mock(),
        commit=AsyncMock(side_effect=RuntimeError("db commit failed")),
        rollback=AsyncMock(),
    )
    payload = {"repeat_on": True, "repeat_seconds": 60}

    with pytest.raises(RuntimeError, match="db commit failed"):
        asyncio.run(
            scheduler._skip_overdue_repeat_and_schedule_next(
                session, post, payload
            )
        )

    session.rollback.assert_awaited_once()
    session.add.assert_called_once()


def test_mark_processing_failure_rolls_back_and_propagates() -> None:
    scheduler = _scheduler()
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("db execute failed")),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="db execute failed"):
        asyncio.run(
            scheduler._mark_processing(
                session, [SimpleNamespace(id=1), SimpleNamespace(id=2)]
            )
        )

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


def test_next_repeat_commit_failure_rolls_back_without_failing_published_post() -> None:
    scheduler = _scheduler()
    scheduled_at = datetime.now(timezone.utc)
    post = SimpleNamespace(id=5, channel_id=9, scheduled_at=scheduled_at)
    session = SimpleNamespace(
        add=Mock(),
        commit=AsyncMock(side_effect=RuntimeError("repeat commit failed")),
        rollback=AsyncMock(),
    )
    payload = {
        "repeat_on": True,
        "repeat_seconds": 60,
        "autodelete_seconds": 30,
    }

    asyncio.run(scheduler._schedule_next_repeat_if_needed(session, post, payload))

    session.add.assert_called_once()
    session.rollback.assert_awaited_once()


def test_autodelete_commit_failure_recovers_session_and_keeps_fallback_payload() -> None:
    scheduler = _scheduler()
    post = SimpleNamespace(
        id=3,
        scheduled_at=datetime.now(timezone.utc),
        payload={},
    )
    session = SimpleNamespace(
        commit=AsyncMock(side_effect=RuntimeError("autodelete commit failed")),
        rollback=AsyncMock(),
    )
    payload = {"autodelete_seconds": 60}

    asyncio.run(
        scheduler._apply_autodelete(
            session,
            post,
            -1001234567890,
            payload,
            [101],
        )
    )

    session.rollback.assert_awaited_once()
    assert payload["autodelete_at"]
    assert post.payload["autodelete_at"] == payload["autodelete_at"]


def test_autodelete_worker_continues_after_iteration_failure() -> None:
    calls = 0

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _factory():
        return _SessionContext()

    scheduler = ReliableScheduler(
        _factory,
        SimpleNamespace(bot=SimpleNamespace()),
        interval_seconds=1,
    )
    scheduler._del_interval_seconds = 0

    async def _process(session) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient deletor failure")
        scheduler._stopping.set()

    scheduler._process_due_deletions = _process

    asyncio.run(scheduler._run_deletor())

    assert calls == 2
