from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.services import canonical_publication_safe_repeat_runtime_control as control
from app.workers.publication_autodelete_views import PublicationAutodeleteViewsWorker
from app.workers.publication_autodelete_views_pin import PublicationAutodeleteViewsPinWorker


class _Loop:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1
        if self.fail_start:
            raise RuntimeError("simulated views+pin worker start failure")

    async def stop(self) -> None:
        self.stopped += 1


def _pin_worker(
    *,
    allow_repeat_views: bool = True,
    allow_repeat_views_pin: bool = True,
) -> PublicationAutodeleteViewsPinWorker:
    return PublicationAutodeleteViewsPinWorker(
        view_source=object(),
        delete_provider=object(),
        allow_repeat_views=allow_repeat_views,
        allow_repeat_views_pin=allow_repeat_views_pin,
    )


def _config() -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(
        _env_file=None,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
    )


def test_construction_only_never_exposes_repeat_views_pin_availability() -> None:
    worker = _pin_worker()
    assert worker.repeat_views_available is False
    assert worker.repeat_views_pin_available is False


def test_successful_exact_worker_start_opens_and_stop_closes_views_pin_availability() -> None:
    async def run() -> None:
        worker = _pin_worker()
        loop = _Loop()
        worker._loop = loop  # type: ignore[assignment]

        await worker.start()
        assert loop.started == 1
        assert worker.repeat_views_available is True
        assert worker.repeat_views_pin_available is True

        await worker.stop()
        assert loop.stopped == 1
        assert worker.repeat_views_available is False
        assert worker.repeat_views_pin_available is False

    asyncio.run(run())


def test_failed_start_never_publishes_repeat_views_pin_availability() -> None:
    async def run() -> None:
        worker = _pin_worker()
        worker._loop = _Loop(fail_start=True)  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="start failure"):
            await worker.start()
        assert worker.repeat_views_available is False
        assert worker.repeat_views_pin_available is False

    asyncio.run(run())


def test_plain_repeat_views_worker_does_not_become_views_pin_capable() -> None:
    async def run() -> None:
        worker = PublicationAutodeleteViewsWorker(
            view_source=object(),
            delete_provider=object(),
            allow_repeat_views=True,
        )
        worker._loop = _Loop()  # type: ignore[assignment]
        await worker.start()
        try:
            assert worker.repeat_views_available is True
            assert getattr(worker, "repeat_views_pin_available", False) is False
        finally:
            await worker.stop()

    asyncio.run(run())


def test_plain_repeat_views_fact_and_pin_fact_do_not_substitute_for_each_other() -> None:
    async def run() -> None:
        plain_only = _pin_worker(
            allow_repeat_views=True,
            allow_repeat_views_pin=False,
        )
        plain_only._loop = _Loop()  # type: ignore[assignment]
        await plain_only.start()
        try:
            assert plain_only.repeat_views_available is True
            assert plain_only.repeat_views_pin_available is False
        finally:
            await plain_only.stop()

        pin_only = _pin_worker(
            allow_repeat_views=False,
            allow_repeat_views_pin=True,
        )
        pin_only._loop = _Loop()  # type: ignore[assignment]
        await pin_only.start()
        try:
            assert pin_only.repeat_views_available is False
            assert pin_only.repeat_views_pin_available is False
        finally:
            await pin_only.stop()

    asyncio.run(run())


def test_runtime_control_never_infers_views_pin_from_broader_availability(monkeypatch) -> None:
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

        monkeypatch.setattr(
            control,
            "build_canonical_publication_safe_repeat_runtime",
            fake_runtime,
        )
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
            repeat_owner_policy_enforced=True,
        )
        assert captured[-1]["allow_repeat"] is True
        assert captured[-1]["allow_views_autodelete"] is True
        assert captured[-1]["allow_repeat_views"] is True
        assert captured[-1]["allow_repeat_views_pin"] is False

    asyncio.run(run())


def test_runtime_control_propagates_views_pin_only_with_exact_complete_fact_set(monkeypatch) -> None:
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

        monkeypatch.setattr(
            control,
            "build_canonical_publication_safe_repeat_runtime",
            fake_runtime,
        )
        monkeypatch.setattr(control, "CanonicalPublicationRepeatHandoffExecutor", FakeRouter)
        monkeypatch.setattr(control, "CanonicalPublicationDeliveryWorker", FakeWorker)

        cases = (
            dict(
                views_autodelete_executor_available=True,
                repeat_continuation_available=True,
                repeat_views_executor_available=False,
                repeat_views_pin_executor_available=True,
                expected=False,
            ),
            dict(
                views_autodelete_executor_available=False,
                repeat_continuation_available=True,
                repeat_views_executor_available=True,
                repeat_views_pin_executor_available=True,
                expected=False,
            ),
            dict(
                views_autodelete_executor_available=True,
                repeat_continuation_available=False,
                repeat_views_executor_available=True,
                repeat_views_pin_executor_available=True,
                expected=False,
            ),
            dict(
                views_autodelete_executor_available=True,
                repeat_continuation_available=True,
                repeat_views_executor_available=True,
                repeat_views_pin_executor_available=True,
                expected=True,
            ),
        )
        for raw in cases:
            case = dict(raw)
            expected = bool(case.pop("expected"))
            await control.start_canonical_publication_safe_repeat_primary_if_enabled(
                config=_config(),
                recovery_worker=object(),
                bot=object(),
                session_factory=object(),  # type: ignore[arg-type]
                repeat_owner_policy_enforced=True,
                **case,
            )
            assert bool(captured[-1]["allow_repeat_views_pin"]) is expected

    asyncio.run(run())
