from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.services import canonical_publication_delivery_runtime_control as control


def _config(**overrides) -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(_env_file=None, **overrides)


def _enabled_config(**overrides) -> CanonicalPublicationDeliveryPrimarySettings:
    values = {
        "CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED": True,
        "CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED": True,
    }
    values.update(overrides)
    return _config(**values)


def test_primary_runtime_disabled_constructs_nothing(monkeypatch) -> None:
    async def run() -> None:
        def unexpected_runtime(**kwargs):
            raise AssertionError("disabled primary must not build runtime")

        class UnexpectedHandoff:
            def __init__(self, **kwargs) -> None:
                raise AssertionError("disabled primary must not construct handoff executor")

        class UnexpectedWorker:
            def __init__(self, **kwargs) -> None:
                raise AssertionError("disabled primary must not construct worker")

        monkeypatch.setattr(
            control,
            "build_canonical_publication_delivery_runtime",
            unexpected_runtime,
        )
        monkeypatch.setattr(
            control,
            "CanonicalPublicationDeliveryHandoffExecutor",
            UnexpectedHandoff,
        )
        monkeypatch.setattr(control, "CanonicalPublicationDeliveryWorker", UnexpectedWorker)

        worker = await control.start_canonical_publication_delivery_primary_if_enabled(
            config=_config(),
            recovery_worker=None,
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
        )
        assert worker is None

    asyncio.run(run())


def test_primary_runtime_requires_started_recovery_before_construction(monkeypatch) -> None:
    async def run() -> None:
        def unexpected_runtime(**kwargs):
            raise AssertionError("runtime must not build without started recovery")

        monkeypatch.setattr(
            control,
            "build_canonical_publication_delivery_runtime",
            unexpected_runtime,
        )

        with pytest.raises(RuntimeError, match="successfully started recovery"):
            await control.start_canonical_publication_delivery_primary_if_enabled(
                config=_enabled_config(),
                recovery_worker=None,
                bot=object(),
                session_factory=object(),  # type: ignore[arg-type]
            )

    asyncio.run(run())


def test_primary_runtime_starts_with_exact_safe_configuration(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}
        executor = object()
        session_factory = object()
        provider = object()

        def fake_runtime(**kwargs):
            captured["runtime"] = kwargs
            return SimpleNamespace(executor=executor)

        class FakeHandoffExecutor:
            def __init__(self, **kwargs) -> None:
                captured["handoff"] = kwargs

        class FakeWorker:
            def __init__(self, **kwargs) -> None:
                captured["worker"] = kwargs
                self.started = False

            async def start(self) -> None:
                self.started = True

            async def stop(self) -> None:
                captured["stopped"] = True

        monkeypatch.setattr(
            control,
            "build_canonical_publication_delivery_runtime",
            fake_runtime,
        )
        monkeypatch.setattr(
            control,
            "CanonicalPublicationDeliveryHandoffExecutor",
            FakeHandoffExecutor,
        )
        monkeypatch.setattr(control, "CanonicalPublicationDeliveryWorker", FakeWorker)

        config = _enabled_config(
            CANONICAL_PUBLICATION_DELIVERY_WORKER_INTERVAL_SECONDS=7,
            CANONICAL_PUBLICATION_DELIVERY_WORKER_BATCH_SIZE=31,
            CANONICAL_PUBLICATION_DELIVERY_WORKER_SCAN_LIMIT=411,
            CANONICAL_PUBLICATION_DELIVERY_WORKER_LEASE_TTL_SECONDS=120,
            CANONICAL_PUBLICATION_DELIVERY_WORKER_HEARTBEAT_INTERVAL_SECONDS=29,
        )
        worker = await control.start_canonical_publication_delivery_primary_if_enabled(
            config=config,
            recovery_worker=object(),
            bot=provider,
            session_factory=session_factory,  # type: ignore[arg-type]
        )

        assert isinstance(worker, FakeWorker)
        assert worker.started is True
        assert captured["runtime"] == {
            "bot": provider,
            "session_factory": session_factory,
            "lease_seconds": 120,
            "heartbeat_interval_seconds": 29.0,
        }
        assert captured["handoff"] == {
            "executor": executor,
            "session_factory": session_factory,
        }
        handoff_executor = captured["worker"]["executor"]  # type: ignore[index]
        assert isinstance(handoff_executor, FakeHandoffExecutor)
        assert captured["worker"] == {
            "executor": handoff_executor,
            "session_factory": session_factory,
            "interval_seconds": 7,
            "batch_size": 31,
            "scan_limit": 411,
        }

    asyncio.run(run())


def test_primary_start_failure_cleans_up_partial_worker(monkeypatch) -> None:
    async def run() -> None:
        calls: list[str] = []

        monkeypatch.setattr(
            control,
            "build_canonical_publication_delivery_runtime",
            lambda **kwargs: SimpleNamespace(executor=object()),
        )

        class FailingWorker:
            def __init__(self, **kwargs) -> None:
                pass

            async def start(self) -> None:
                calls.append("start")
                raise RuntimeError("startup failed")

            async def stop(self) -> None:
                calls.append("stop")

        monkeypatch.setattr(control, "CanonicalPublicationDeliveryWorker", FailingWorker)

        with pytest.raises(RuntimeError, match="startup failed"):
            await control.start_canonical_publication_delivery_primary_if_enabled(
                config=_enabled_config(),
                recovery_worker=object(),
                bot=object(),
                session_factory=object(),  # type: ignore[arg-type]
            )

        assert calls == ["start", "stop"]

    asyncio.run(run())


def test_shutdown_stops_primary_before_recovery_even_after_primary_error() -> None:
    async def run() -> None:
        calls: list[str] = []

        class Primary:
            async def stop(self) -> None:
                calls.append("primary")
                raise RuntimeError("primary stop failed")

        class Recovery:
            async def stop(self) -> None:
                calls.append("recovery")

        await control.stop_canonical_publication_delivery_workers(
            primary_worker=Primary(),
            recovery_worker=Recovery(),
        )

        assert calls == ["primary", "recovery"]

    asyncio.run(run())
