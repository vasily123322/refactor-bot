from __future__ import annotations

import asyncio


def test_dispatcher_does_not_construct_delivery_recovery_worker_when_disabled(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        class UnexpectedWorker:
            def __init__(self, **kwargs) -> None:
                raise AssertionError("disabled recovery worker must not be constructed")

        monkeypatch.setattr(
            dispatcher,
            "CanonicalPublicationDeliveryRecoveryWorker",
            UnexpectedWorker,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "canonical_publication_delivery_recovery_worker_enabled",
            False,
        )

        worker = (
            await dispatcher._start_canonical_publication_delivery_recovery_worker_if_enabled()
        )
        assert worker is None

    asyncio.run(run())


def test_dispatcher_starts_delivery_recovery_worker_with_explicit_configuration(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        captured: dict[str, object] = {}

        class FakeWorker:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)
                self.started = False

            async def start(self) -> None:
                self.started = True

        monkeypatch.setattr(
            dispatcher,
            "CanonicalPublicationDeliveryRecoveryWorker",
            FakeWorker,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "canonical_publication_delivery_recovery_worker_enabled",
            True,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "canonical_publication_delivery_recovery_worker_interval_seconds",
            75,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "canonical_publication_delivery_recovery_worker_batch_size",
            37,
        )

        worker = (
            await dispatcher._start_canonical_publication_delivery_recovery_worker_if_enabled()
        )

        assert isinstance(worker, FakeWorker)
        assert worker.started is True
        assert captured == {
            "session_factory": dispatcher.AsyncSessionLocal,
            "interval_seconds": 75,
            "batch_size": 37,
        }

    asyncio.run(run())
