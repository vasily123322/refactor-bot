from __future__ import annotations

import asyncio

import pytest

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)


def _config() -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(
        _env_file=None,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
    )


def test_dispatcher_starts_recovery_before_primary_and_passes_dependency_facts(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        calls: list[str] = []
        recovery = object()
        primary = object()
        config = _config()

        async def start_recovery():
            calls.append("recovery")
            return recovery

        async def start_primary(**kwargs):
            calls.append("primary")
            assert kwargs["config"] is config
            assert kwargs["recovery_worker"] is recovery
            assert kwargs["bot"] is dispatcher.bot
            assert kwargs["session_factory"] is dispatcher.AsyncSessionLocal
            assert kwargs["time_autodelete_executor_available"] is True
            assert kwargs["views_autodelete_executor_available"] is True
            assert kwargs["repeat_continuation_available"] is True
            assert kwargs["repeat_owner_policy_enforced"] is True
            return primary

        async def unexpected_stop(**kwargs):
            raise AssertionError("successful startup must not run cleanup")

        monkeypatch.setattr(
            dispatcher,
            "_start_canonical_publication_delivery_recovery_worker_if_enabled",
            start_recovery,
        )
        monkeypatch.setattr(
            dispatcher,
            "start_canonical_publication_safe_repeat_primary_if_enabled",
            start_primary,
        )
        monkeypatch.setattr(
            dispatcher,
            "stop_canonical_publication_delivery_workers",
            unexpected_stop,
        )

        result = await dispatcher._start_canonical_publication_delivery_workers(
            config,
            time_autodelete_executor_available=True,
            views_autodelete_executor_available=True,
            repeat_continuation_executor_available=True,
        )

        assert result == (primary, recovery)
        assert calls == ["recovery", "primary"]

    asyncio.run(run())


def test_dispatcher_repeat_dependency_defaults_false_and_failure_stops_recovery(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        recovery = object()
        cleanup: list[dict[str, object | None]] = []

        async def start_recovery():
            return recovery

        async def fail_primary(**kwargs):
            assert kwargs["recovery_worker"] is recovery
            assert kwargs["time_autodelete_executor_available"] is False
            assert kwargs["views_autodelete_executor_available"] is False
            assert kwargs["repeat_continuation_available"] is False
            assert kwargs["repeat_owner_policy_enforced"] is True
            raise RuntimeError("primary startup failed")

        async def stop_workers(**kwargs):
            cleanup.append(dict(kwargs))

        monkeypatch.setattr(
            dispatcher,
            "_start_canonical_publication_delivery_recovery_worker_if_enabled",
            start_recovery,
        )
        monkeypatch.setattr(
            dispatcher,
            "start_canonical_publication_safe_repeat_primary_if_enabled",
            fail_primary,
        )
        monkeypatch.setattr(
            dispatcher,
            "stop_canonical_publication_delivery_workers",
            stop_workers,
        )

        with pytest.raises(RuntimeError, match="primary startup failed"):
            await dispatcher._start_canonical_publication_delivery_workers(_config())

        assert cleanup == [
            {
                "primary_worker": None,
                "recovery_worker": recovery,
            }
        ]

    asyncio.run(run())


def test_dispatcher_views_worker_requires_userbot_and_reports_only_successful_start(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )

        class UnexpectedWorker:
            def __init__(self, **kwargs) -> None:
                raise AssertionError("views worker must not construct without userbot")

        monkeypatch.setattr(dispatcher, "PublicationAutodeleteViewsWorker", UnexpectedWorker)
        assert (
            await dispatcher._start_publication_autodelete_views_worker_if_enabled(
                userbot_available=False,
            )
            is None
        )

        events: list[str] = []

        class StartedWorker:
            def __init__(self, **kwargs) -> None:
                events.append("construct")

            async def start(self) -> None:
                events.append("start")

        monkeypatch.setattr(dispatcher, "PublicationAutodeleteViewsWorker", StartedWorker)
        worker = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
        )
        assert isinstance(worker, StartedWorker)
        assert events == ["construct", "start"]

    asyncio.run(run())


def test_dispatcher_recovery_start_failure_cleans_partial_worker(monkeypatch) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        calls: list[str] = []

        class FailingRecoveryWorker:
            def __init__(self, **kwargs) -> None:
                pass

            async def start(self) -> None:
                calls.append("start")
                raise RuntimeError("recovery startup failed")

            async def stop(self) -> None:
                calls.append("stop")

        monkeypatch.setattr(
            dispatcher,
            "CanonicalPublicationDeliveryRecoveryWorker",
            FailingRecoveryWorker,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "canonical_publication_delivery_recovery_worker_enabled",
            True,
        )

        with pytest.raises(RuntimeError, match="recovery startup failed"):
            await dispatcher._start_canonical_publication_delivery_recovery_worker_if_enabled()

        assert calls == ["start", "stop"]

    asyncio.run(run())
