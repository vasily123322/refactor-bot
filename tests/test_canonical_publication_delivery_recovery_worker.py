from __future__ import annotations

import asyncio

from app.services.canonical_publication_delivery_recovery import (
    CanonicalPublicationDeliveryRecoveryTick,
)
from app.workers import canonical_publication_delivery_recovery as worker_module
from app.workers.canonical_publication_delivery_recovery import (
    CanonicalPublicationDeliveryRecoveryWorker,
)


def test_recovery_worker_bounds_polling_configuration() -> None:
    worker = CanonicalPublicationDeliveryRecoveryWorker(
        session_factory=lambda: None,  # type: ignore[arg-type]
        interval_seconds=1,
        batch_size=9999,
    )

    assert worker.interval_seconds == 30
    assert worker.batch_size == 500

    fallback = CanonicalPublicationDeliveryRecoveryWorker(
        session_factory=lambda: None,  # type: ignore[arg-type]
        interval_seconds=None,  # type: ignore[arg-type]
        batch_size=None,  # type: ignore[arg-type]
    )
    assert fallback.interval_seconds == 60
    assert fallback.batch_size == 100


def test_recovery_worker_delegates_one_bounded_tick_without_provider(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}
        session_factory = object()

        class FakeRecoveryService:
            def __init__(self, factory) -> None:
                captured["session_factory"] = factory

            async def run_once(self, *, batch_size: int):
                captured["batch_size"] = batch_size
                return CanonicalPublicationDeliveryRecoveryTick(
                    selected=3,
                    taken_over=2,
                    failed_unknown=2,
                    contention=1,
                )

        monkeypatch.setattr(
            worker_module,
            "CanonicalPublicationDeliveryRecoveryService",
            FakeRecoveryService,
        )
        worker = CanonicalPublicationDeliveryRecoveryWorker(
            session_factory=session_factory,  # type: ignore[arg-type]
            batch_size=37,
        )

        tick = await worker.run_once()

        assert captured == {
            "session_factory": session_factory,
            "batch_size": 37,
        }
        assert tick.selected == 3
        assert tick.failed_unknown == 2
        assert tick.contention == 1

    asyncio.run(run())


def test_recovery_worker_tick_surfaces_service_cancellation(monkeypatch) -> None:
    async def run() -> None:
        class CancelledRecoveryService:
            def __init__(self, factory) -> None:
                pass

            async def run_once(self, *, batch_size: int):
                raise asyncio.CancelledError

        monkeypatch.setattr(
            worker_module,
            "CanonicalPublicationDeliveryRecoveryService",
            CancelledRecoveryService,
        )
        worker = CanonicalPublicationDeliveryRecoveryWorker(
            session_factory=object(),  # type: ignore[arg-type]
        )

        try:
            await worker.run_once()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("recovery worker must propagate cancellation")

    asyncio.run(run())
