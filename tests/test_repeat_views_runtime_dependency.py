from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.services import canonical_publication_safe_repeat_runtime_control as control
from app.services.canonical_publication_safe_repeat_runtime import (
    build_canonical_publication_safe_repeat_runtime,
)
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle
from app.workers import publication_autodelete_views as worker_module
from app.workers.publication_autodelete_views import PublicationAutodeleteViewsWorker


class _Bot:
    def __getattr__(self, name):
        raise AssertionError(f"runtime construction must not call bot method {name}")


class _Loop:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.fail:
            raise RuntimeError("loop start failed")

    async def stop(self) -> None:
        self.stop_calls += 1


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessionFactory:
    def __call__(self):
        return _SessionContext()


def test_repeat_views_worker_availability_requires_successful_start_and_explicit_mode() -> None:
    async def run() -> None:
        enabled = PublicationAutodeleteViewsWorker(
            view_source=object(),
            delete_provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
            allow_repeat_views=True,
        )
        loop = _Loop()
        enabled._loop = loop  # type: ignore[assignment]
        assert enabled.repeat_views_available is False
        await enabled.start()
        assert enabled.repeat_views_available is True
        await enabled.stop()
        assert enabled.repeat_views_available is False
        assert loop.start_calls == 1
        assert loop.stop_calls == 1

        default_off = PublicationAutodeleteViewsWorker(
            view_source=object(),
            delete_provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
        )
        default_loop = _Loop()
        default_off._loop = default_loop  # type: ignore[assignment]
        await default_off.start()
        assert default_off.allow_repeat_views is False
        assert default_off.repeat_views_available is False
        await default_off.stop()

        failed = PublicationAutodeleteViewsWorker(
            view_source=object(),
            delete_provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
            allow_repeat_views=True,
        )
        failed._loop = _Loop(fail=True)  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="loop start failed"):
            await failed.start()
        assert failed.repeat_views_available is False

    asyncio.run(run())


def test_repeat_capable_worker_passes_exact_mode_and_lease_into_service(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}
        handle = PublicationAutodeleteLeaseHandle(
            publication_id=17,
            lease_token="lease-token",
            holder="views-worker",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )

        class FakeMixedService:
            def __init__(self, session, **kwargs) -> None:
                captured["mixed_session"] = session
                captured["mixed_kwargs"] = kwargs

            async def views_evaluate_and_delete(
                self,
                publication_id: int,
                *,
                lease,
                now,
            ):
                captured["mixed_publication_id"] = publication_id
                captured["mixed_lease"] = lease
                return None

        class FakeService:
            def __init__(self, session, **kwargs) -> None:
                captured["session"] = session
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
            "PublicationMixedAutodeleteService",
            FakeMixedService,
        )
        monkeypatch.setattr(
            worker_module,
            "PublicationAutodeleteViewsService",
            FakeService,
        )

        worker = PublicationAutodeleteViewsWorker(
            view_source=object(),
            delete_provider=object(),
            session_factory=_SessionFactory(),  # type: ignore[arg-type]
            allow_repeat_views=True,
        )

        async def select(*, now):
            return SimpleNamespace(publication_ids=(17,))

        async def acquire(publication_id: int):
            assert publication_id == 17
            return handle

        async def release(candidate):
            assert candidate is handle
            return True

        worker._select = select  # type: ignore[method-assign]
        worker._acquire = acquire  # type: ignore[method-assign]
        worker._release = release  # type: ignore[method-assign]

        tick = await worker.run_once()
        assert tick.leased == 1
        assert tick.ineligible == 1
        assert captured["mixed_publication_id"] == 17
        assert captured["mixed_lease"] is handle
        assert captured["publication_id"] == 17
        kwargs = captured["kwargs"]
        assert kwargs["allow_repeat_views"] is True  # type: ignore[index]
        assert kwargs["lease"] is handle  # type: ignore[index]

    asyncio.run(run())


def test_safe_runtime_repeat_views_fact_is_independent_and_compound() -> None:
    fully_proven = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_views_autodelete=True,
        allow_repeat=True,
        allow_repeat_views=True,
        repeat_owner_policy_enforced=True,
    )
    assert fully_proven.executor.allow_repeat is True
    assert fully_proven.executor.allow_views_autodelete is True
    assert fully_proven.executor.allow_repeat_views is True

    no_views = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_views_autodelete=False,
        allow_repeat=True,
        allow_repeat_views=True,
        repeat_owner_policy_enforced=True,
    )
    assert no_views.executor.allow_repeat_views is False

    no_continuation = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_views_autodelete=True,
        allow_repeat=False,
        allow_repeat_views=True,
        repeat_owner_policy_enforced=True,
    )
    assert no_continuation.executor.allow_repeat is False
    assert no_continuation.executor.allow_repeat_views is False

    no_owner_policy = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_views_autodelete=True,
        allow_repeat=True,
        allow_repeat_views=True,
        repeat_owner_policy_enforced=False,
    )
    assert no_owner_policy.executor.allow_repeat is False
    assert no_owner_policy.executor.allow_repeat_views is False


def test_primary_control_never_infers_repeat_views_from_ordinary_views(monkeypatch) -> None:
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

        config = CanonicalPublicationDeliveryPrimarySettings(
            _env_file=None,
            CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
            CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
        )

        await control.start_canonical_publication_safe_repeat_primary_if_enabled(
            config=config,
            recovery_worker=object(),
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
            views_autodelete_executor_available=True,
            repeat_continuation_available=True,
            repeat_owner_policy_enforced=True,
        )
        assert captured[-1]["allow_views_autodelete"] is True
        assert captured[-1]["allow_repeat"] is True
        assert captured[-1]["allow_repeat_views"] is False

        await control.start_canonical_publication_safe_repeat_primary_if_enabled(
            config=config,
            recovery_worker=object(),
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
            views_autodelete_executor_available=True,
            repeat_continuation_available=True,
            repeat_views_executor_available=True,
            repeat_owner_policy_enforced=True,
        )
        assert captured[-1]["allow_repeat_views"] is True

    asyncio.run(run())
