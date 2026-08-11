from __future__ import annotations

import asyncio

import pytest

from app.workers import canonical_repeat_continuation_scheduler as module


def test_repeat_continuation_disabled_preserves_parent_scheduler_only(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []

        async def parent_start(self) -> None:
            events.append("parent-start")

        async def parent_stop(self) -> None:
            events.append("parent-stop")

        def unexpected_factory(**kwargs):
            raise AssertionError("disabled continuation must not construct worker")

        monkeypatch.setattr(module.RecoveryScheduler, "start", parent_start)
        monkeypatch.setattr(module.RecoveryScheduler, "stop", parent_stop)
        scheduler = module.Scheduler(
            object(),
            object(),
            repeat_continuation_enabled=False,
            continuation_worker_factory=unexpected_factory,
        )

        await scheduler.start()
        assert scheduler.repeat_continuation_available is False
        await scheduler.stop()
        assert events == ["parent-start", "parent-stop"]

    asyncio.run(run())


def test_repeat_continuation_started_worker_exposes_availability_and_stops_first(
    monkeypatch,
) -> None:
    async def run() -> None:
        events: list[str] = []
        session_factory = object()

        async def parent_start(self) -> None:
            events.append("parent-start")

        async def parent_stop(self) -> None:
            events.append("parent-stop")

        class Worker:
            async def start(self) -> None:
                events.append("continuation-start")

            async def stop(self) -> None:
                events.append("continuation-stop")

        def factory(**kwargs):
            assert kwargs == {"session_factory": session_factory}
            return Worker()

        monkeypatch.setattr(module.RecoveryScheduler, "start", parent_start)
        monkeypatch.setattr(module.RecoveryScheduler, "stop", parent_stop)
        scheduler = module.Scheduler(
            session_factory,
            object(),
            repeat_continuation_enabled=True,
            continuation_worker_factory=factory,
        )

        await scheduler.start()
        assert scheduler.repeat_continuation_available is True
        await scheduler.stop()
        assert scheduler.repeat_continuation_available is False
        assert events == [
            "parent-start",
            "continuation-start",
            "continuation-stop",
            "parent-stop",
        ]

    asyncio.run(run())


def test_repeat_continuation_start_failure_stops_partial_worker_and_parent(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []

        async def parent_start(self) -> None:
            events.append("parent-start")

        async def parent_stop(self) -> None:
            events.append("parent-stop")

        class Worker:
            async def start(self) -> None:
                events.append("continuation-start")
                raise RuntimeError("continuation startup failed")

            async def stop(self) -> None:
                events.append("continuation-stop")

        monkeypatch.setattr(module.RecoveryScheduler, "start", parent_start)
        monkeypatch.setattr(module.RecoveryScheduler, "stop", parent_stop)
        scheduler = module.Scheduler(
            object(),
            object(),
            repeat_continuation_enabled=True,
            continuation_worker_factory=lambda **kwargs: Worker(),
        )

        with pytest.raises(RuntimeError, match="continuation startup failed"):
            await scheduler.start()
        assert scheduler.repeat_continuation_available is False
        assert events == [
            "parent-start",
            "continuation-start",
            "continuation-stop",
            "parent-stop",
        ]

    asyncio.run(run())


def test_repeat_continuation_requires_factory_backed_scheduler(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []

        async def parent_start(self) -> None:
            events.append("parent-start")

        async def parent_stop(self) -> None:
            events.append("parent-stop")

        monkeypatch.setattr(module.RecoveryScheduler, "start", parent_start)
        monkeypatch.setattr(module.RecoveryScheduler, "stop", parent_stop)

        # The parent identifies an AsyncSession instance specially. Force the same
        # unsupported single-session shape without launching real scheduler loops.
        scheduler = module.Scheduler(
            object(),
            object(),
            repeat_continuation_enabled=True,
            continuation_worker_factory=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("single-session scheduler must not construct continuation")
            ),
        )
        scheduler.session_factory = None

        await scheduler.start()
        assert scheduler.repeat_continuation_available is False
        await scheduler.stop()
        assert events == ["parent-start", "parent-stop"]

    asyncio.run(run())
