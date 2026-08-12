from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.services import canonical_publication_safe_repeat_runtime_control as control
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle
from app.workers import publication_autodelete_views_pin_forward as worker_module
from app.workers.publication_autodelete_views_forward import PublicationAutodeleteViewsForwardWorker
from app.workers.publication_autodelete_views_pin_forward import (
    PublicationAutodeleteViewsPinForwardWorker,
)


class _Loop:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("combined start failure")

    async def stop(self) -> None:
        return None


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessionFactory:
    def __call__(self):
        return _SessionContext()


def _worker(
    *,
    repeat_views: bool = True,
    pin: bool = True,
    forward: bool = True,
    combined: bool = True,
) -> PublicationAutodeleteViewsPinForwardWorker:
    return PublicationAutodeleteViewsPinForwardWorker(
        view_source=object(),
        delete_provider=object(),
        allow_repeat_views=repeat_views,
        allow_repeat_views_pin=pin,
        allow_repeat_views_forward=forward,
        allow_repeat_views_pin_forward=combined,
    )


def _config() -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(
        _env_file=None,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
    )


def test_combined_availability_requires_exact_successful_started_worker() -> None:
    async def run() -> None:
        worker = _worker()
        assert worker.repeat_views_pin_forward_available is False
        worker._loop = _Loop()  # type: ignore[assignment]
        await worker.start()
        assert worker.repeat_views_available is True
        assert worker.repeat_views_pin_available is True
        assert worker.repeat_views_forward_available is True
        assert worker.repeat_views_pin_forward_available is True
        await worker.stop()
        assert worker.repeat_views_pin_forward_available is False

    asyncio.run(run())


def test_failed_start_and_missing_any_underlying_mode_never_publish_combined() -> None:
    async def run() -> None:
        failed = _worker()
        failed._loop = _Loop(fail_start=True)  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="start failure"):
            await failed.start()
        assert failed.repeat_views_pin_forward_available is False

        cases = (
            dict(repeat_views=False, pin=True, forward=True, combined=True),
            dict(repeat_views=True, pin=False, forward=True, combined=True),
            dict(repeat_views=True, pin=True, forward=False, combined=True),
            dict(repeat_views=True, pin=True, forward=True, combined=False),
        )
        for case in cases:
            worker = _worker(**case)
            worker._loop = _Loop()  # type: ignore[assignment]
            await worker.start()
            try:
                assert worker.repeat_views_pin_forward_available is False
            finally:
                await worker.stop()

    asyncio.run(run())


def test_existing_forward_worker_never_infers_combined_from_pin_and_forward() -> None:
    async def run() -> None:
        worker = PublicationAutodeleteViewsForwardWorker(
            view_source=object(),
            delete_provider=object(),
            allow_repeat_views=True,
            allow_repeat_views_pin=True,
            allow_repeat_views_forward=True,
        )
        worker._loop = _Loop()  # type: ignore[assignment]
        await worker.start()
        try:
            assert worker.repeat_views_pin_available is True
            assert worker.repeat_views_forward_available is True
            assert getattr(worker, "repeat_views_pin_forward_available", False) is False
        finally:
            await worker.stop()

    asyncio.run(run())


def test_combined_worker_passes_exact_fact_and_lease_to_current_service(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}
        handle = PublicationAutodeleteLeaseHandle(
            publication_id=71,
            lease_token="combined-lease-token",
            holder="combined-worker",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )

        class FakeService:
            def __init__(self, session, **kwargs) -> None:
                captured["kwargs"] = kwargs

            async def evaluate_and_delete(self, publication_id: int, *, now):
                captured["publication_id"] = publication_id
                return SimpleNamespace(
                    outcome="ineligible",
                    threshold=None,
                    ambiguous_count=0,
                )

        monkeypatch.setattr(
            worker_module,
            "PublicationAutodeleteViewsComposedService",
            FakeService,
        )
        worker = PublicationAutodeleteViewsPinForwardWorker(
            view_source=object(),
            delete_provider=object(),
            session_factory=_SessionFactory(),  # type: ignore[arg-type]
            allow_repeat_views=True,
            allow_repeat_views_pin=True,
            allow_repeat_views_forward=True,
            allow_repeat_views_pin_forward=True,
        )

        async def select(*, now):
            return SimpleNamespace(publication_ids=(71,))

        async def acquire(publication_id: int):
            return handle

        async def release(candidate):
            assert candidate is handle
            return True

        worker._select = select  # type: ignore[method-assign]
        worker._acquire = acquire  # type: ignore[method-assign]
        worker._release = release  # type: ignore[method-assign]
        tick = await worker.run_once()
        assert tick.leased == 1
        kwargs = captured["kwargs"]
        assert kwargs["allow_repeat_views"] is True  # type: ignore[index]
        assert kwargs["allow_repeat_views_pin"] is True  # type: ignore[index]
        assert kwargs["allow_repeat_views_forward"] is True  # type: ignore[index]
        assert kwargs["allow_repeat_views_pin_forward"] is True  # type: ignore[index]
        assert kwargs["lease"] is handle  # type: ignore[index]

    asyncio.run(run())


def test_runtime_control_never_infers_combined_and_requires_complete_started_lattice(monkeypatch) -> None:
    async def run() -> None:
        captured: list[dict[str, object]] = []

        def fake_runtime(**kwargs):
            captured.append(dict(kwargs))
            return SimpleNamespace(executor=object())

        class FakeRouter:
            def __init__(self, **kwargs) -> None:
                pass

        class FakeWorker:
            def __init__(self, **kwargs) -> None:
                pass

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

        monkeypatch.setattr(control, "build_canonical_publication_safe_repeat_runtime", fake_runtime)
        monkeypatch.setattr(control, "CanonicalPublicationRepeatHandoffExecutor", FakeRouter)
        monkeypatch.setattr(control, "CanonicalPublicationDeliveryWorker", FakeWorker)

        await control.start_canonical_publication_safe_repeat_primary_if_enabled(
            config=_config(),
            recovery_worker=object(),
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
            views_autodelete_executor_available=True,
            repeat_continuation_available=True,
            repeat_views_executor_available=True,
            repeat_views_pin_executor_available=True,
            repeat_views_forward_executor_available=True,
            repeat_owner_policy_enforced=True,
        )
        assert captured[-1]["allow_repeat_views_pin_forward"] is False

        cases = (
            dict(pin=True, forward=True, combined=True, expected=True),
            dict(pin=False, forward=True, combined=True, expected=False),
            dict(pin=True, forward=False, combined=True, expected=False),
            dict(pin=True, forward=True, combined=False, expected=False),
        )
        for case in cases:
            await control.start_canonical_publication_safe_repeat_primary_if_enabled(
                config=_config(),
                recovery_worker=object(),
                bot=object(),
                session_factory=object(),  # type: ignore[arg-type]
                views_autodelete_executor_available=True,
                repeat_continuation_available=True,
                repeat_views_executor_available=True,
                repeat_views_pin_executor_available=case["pin"],
                repeat_views_forward_executor_available=case["forward"],
                repeat_views_pin_forward_executor_available=case["combined"],
                repeat_owner_policy_enforced=True,
            )
            assert bool(captured[-1]["allow_repeat_views_pin_forward"]) is case["expected"]

    asyncio.run(run())
