from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.services import canonical_publication_safe_repeat_runtime_control as control
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.canonical_publication_safe_repeat_runtime import (
    build_canonical_publication_safe_repeat_runtime,
)


class _Bot:
    def __getattr__(self, name):
        raise AssertionError(f"runtime construction must not call bot method {name}")


def test_safe_repeat_runtime_needs_both_continuation_and_owner_policy() -> None:
    bot = _Bot()
    session_factory = object()

    blocked = build_canonical_publication_safe_repeat_runtime(
        bot=bot,
        session_factory=session_factory,  # type: ignore[arg-type]
        allow_repeat=True,
        repeat_owner_policy_enforced=False,
    )
    assert isinstance(blocked.executor, CanonicalPublicationSafeRepeatDeliveryExecutor)
    assert blocked.executor.allow_repeat is False

    allowed = build_canonical_publication_safe_repeat_runtime(
        bot=bot,
        session_factory=session_factory,  # type: ignore[arg-type]
        allow_repeat=True,
        repeat_owner_policy_enforced=True,
    )
    assert isinstance(allowed.executor, CanonicalPublicationSafeRepeatDeliveryExecutor)
    assert allowed.executor.allow_repeat is True

    no_continuation = build_canonical_publication_safe_repeat_runtime(
        bot=bot,
        session_factory=session_factory,  # type: ignore[arg-type]
        allow_repeat=False,
        repeat_owner_policy_enforced=True,
    )
    assert no_continuation.executor.allow_repeat is False


def test_safe_repeat_runtime_preserves_nonrepeat_delete_dependency_flags() -> None:
    runtime = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_time_autodelete=True,
        allow_views_autodelete=True,
        allow_repeat=False,
    )
    assert runtime.executor.allow_time_autodelete is True
    assert runtime.executor.allow_views_autodelete is True
    assert runtime.executor.allow_repeat is False


def test_safe_repeat_primary_control_passes_two_factor_repeat_gate(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}
        executor = SimpleNamespace()

        def fake_runtime(**kwargs):
            captured["runtime"] = kwargs
            return SimpleNamespace(executor=executor)

        class FakeRouter:
            def __init__(self, **kwargs) -> None:
                captured["router"] = kwargs

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
        worker = await control.start_canonical_publication_safe_repeat_primary_if_enabled(
            config=config,
            recovery_worker=object(),
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
            time_autodelete_executor_available=True,
            views_autodelete_executor_available=True,
            repeat_continuation_available=True,
            repeat_owner_policy_enforced=False,
        )
        assert isinstance(worker, FakeWorker)
        assert worker.started is True
        assert captured["runtime"]["allow_repeat"] is True  # type: ignore[index]
        assert captured["runtime"]["repeat_owner_policy_enforced"] is False  # type: ignore[index]
        assert captured["runtime"]["allow_time_autodelete"] is True  # type: ignore[index]
        assert captured["runtime"]["allow_views_autodelete"] is True  # type: ignore[index]
        assert captured["router"] == {  # type: ignore[comparison-overlap]
            "executor": executor,
            "session_factory": captured["runtime"]["session_factory"],  # type: ignore[index]
        }

    asyncio.run(run())
