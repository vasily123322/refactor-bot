from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.services import canonical_publication_safe_repeat_runtime_control as control
from app.services.canonical_publication_safe_repeat_runtime import (
    build_canonical_publication_safe_repeat_runtime,
)


class _Bot:
    def __getattr__(self, name):
        raise AssertionError(f"runtime construction must not call bot method {name}")


def _config() -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(
        _env_file=None,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
    )


def test_safe_runtime_repeat_time_requires_all_compound_dependencies() -> None:
    enabled = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_time_autodelete=True,
        allow_repeat=True,
        allow_repeat_time=True,
        repeat_owner_policy_enforced=True,
    )
    assert enabled.executor.allow_time_autodelete is True
    assert enabled.executor.allow_repeat is True
    assert enabled.executor.allow_repeat_time is True

    no_time = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_time_autodelete=False,
        allow_repeat=True,
        allow_repeat_time=True,
        repeat_owner_policy_enforced=True,
    )
    assert no_time.executor.allow_repeat_time is False

    no_repeat = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_time_autodelete=True,
        allow_repeat=False,
        allow_repeat_time=True,
        repeat_owner_policy_enforced=True,
    )
    assert no_repeat.executor.allow_repeat_time is False

    no_owner_policy = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_time_autodelete=True,
        allow_repeat=True,
        allow_repeat_time=True,
        repeat_owner_policy_enforced=False,
    )
    assert no_owner_policy.executor.allow_repeat_time is False


def test_primary_control_never_infers_repeat_time_from_generic_time(monkeypatch) -> None:
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
            time_autodelete_executor_available=True,
            repeat_continuation_available=True,
            repeat_owner_policy_enforced=True,
        )
        assert captured[-1]["allow_time_autodelete"] is True
        assert captured[-1]["allow_repeat"] is True
        assert captured[-1]["allow_repeat_time"] is False

        await control.start_canonical_publication_safe_repeat_primary_if_enabled(
            config=_config(),
            recovery_worker=object(),
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
            time_autodelete_executor_available=True,
            repeat_continuation_available=True,
            repeat_time_executor_available=True,
            repeat_owner_policy_enforced=True,
        )
        assert captured[-1]["allow_repeat_time"] is True

        await control.start_canonical_publication_safe_repeat_primary_if_enabled(
            config=_config(),
            recovery_worker=object(),
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
            time_autodelete_executor_available=False,
            repeat_continuation_available=True,
            repeat_time_executor_available=True,
            repeat_owner_policy_enforced=True,
        )
        assert captured[-1]["allow_repeat_time"] is False

    asyncio.run(run())


def test_dispatcher_repeat_time_worker_and_primary_use_only_started_fact(monkeypatch) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_worker_enabled",
            True,
        )
        events: list[str] = []

        class FakeRepeatTimeWorker:
            def __init__(self, **kwargs) -> None:
                events.append("construct")
                assert kwargs["allow_repeat_time"] is True
                self.repeat_time_available = False

            async def start(self) -> None:
                events.append("start")
                self.repeat_time_available = True

            async def stop(self) -> None:
                events.append("stop")
                self.repeat_time_available = False

        monkeypatch.setattr(
            dispatcher,
            "CanonicalRepeatTimeAutodeleteWorker",
            FakeRepeatTimeWorker,
        )

        assert (
            await dispatcher._start_canonical_repeat_time_autodelete_worker_if_enabled(
                repeat_continuation_available=False,
            )
            is None
        )
        assert events == []

        worker = await dispatcher._start_canonical_repeat_time_autodelete_worker_if_enabled(
            repeat_continuation_available=True,
        )
        assert worker is not None
        assert worker.repeat_time_available is True
        assert events == ["construct", "start"]

        recovery = object()
        primary = object()
        captured: dict[str, object] = {}

        async def start_recovery():
            return recovery

        async def start_primary(**kwargs):
            captured.update(kwargs)
            return primary

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

        result = await dispatcher._start_canonical_publication_delivery_workers(
            _config(),
            time_autodelete_executor_available=True,
            repeat_continuation_executor_available=True,
            repeat_time_executor_available=worker.repeat_time_available,
        )
        assert result == (primary, recovery)
        assert captured["time_autodelete_executor_available"] is True
        assert captured["repeat_continuation_available"] is True
        assert captured["repeat_time_executor_available"] is True

    asyncio.run(run())
