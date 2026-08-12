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


def test_combined_runtime_requires_both_narrower_facts_and_dedicated_fact() -> None:
    enabled = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_time_autodelete=True,
        allow_repeat=True,
        allow_repeat_time=True,
        allow_repeat_time_pin=True,
        allow_repeat_time_forward=True,
        allow_repeat_time_pin_forward=True,
        repeat_owner_policy_enforced=True,
    )
    assert enabled.executor.allow_repeat_time_pin_forward is True

    for overrides in (
        {"allow_repeat_time_pin": False},
        {"allow_repeat_time_forward": False},
        {"allow_repeat_time_pin_forward": False},
    ):
        kwargs = {
            "allow_time_autodelete": True,
            "allow_repeat": True,
            "allow_repeat_time": True,
            "allow_repeat_time_pin": True,
            "allow_repeat_time_forward": True,
            "allow_repeat_time_pin_forward": True,
            "repeat_owner_policy_enforced": True,
        }
        kwargs.update(overrides)
        runtime = build_canonical_publication_safe_repeat_runtime(
            bot=_Bot(),
            session_factory=object(),  # type: ignore[arg-type]
            **kwargs,
        )
        assert runtime.executor.allow_repeat_time_pin_forward is False


def test_primary_control_does_not_infer_combined_from_both_narrower_workers(
    monkeypatch,
) -> None:
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

        common = dict(
            config=_config(),
            recovery_worker=object(),
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
            time_autodelete_executor_available=True,
            repeat_continuation_available=True,
            repeat_time_executor_available=True,
            repeat_time_pin_executor_available=True,
            repeat_time_forward_executor_available=True,
            repeat_owner_policy_enforced=True,
        )
        await control.start_canonical_publication_safe_repeat_primary_if_enabled(**common)
        assert captured[-1]["allow_repeat_time_pin"] is True
        assert captured[-1]["allow_repeat_time_forward"] is True
        assert captured[-1]["allow_repeat_time_pin_forward"] is False

        await control.start_canonical_publication_safe_repeat_primary_if_enabled(
            **common,
            repeat_time_pin_forward_executor_available=True,
        )
        assert captured[-1]["allow_repeat_time_pin_forward"] is True

    asyncio.run(run())


def test_dispatcher_combined_worker_requires_both_started_narrower_workers(monkeypatch) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(dispatcher.settings, "publication_autodelete_worker_enabled", True)
        events: list[str] = []

        class FakeCombinedWorker:
            def __init__(self, **kwargs) -> None:
                events.append("construct")
                assert kwargs["allow_repeat_time_pin_forward"] is True
                self.repeat_time_pin_forward_available = False

            async def start(self) -> None:
                events.append("start")
                self.repeat_time_pin_forward_available = True

            async def stop(self) -> None:
                events.append("stop")
                self.repeat_time_pin_forward_available = False

        monkeypatch.setattr(
            dispatcher,
            "CanonicalRepeatTimePinForwardAutodeleteWorker",
            FakeCombinedWorker,
        )

        for pin_available, forward_available in ((False, True), (True, False)):
            assert (
                await dispatcher._start_canonical_repeat_time_pin_forward_autodelete_worker_if_enabled(
                    repeat_continuation_available=True,
                    repeat_time_pin_available=pin_available,
                    repeat_time_forward_available=forward_available,
                )
                is None
            )
        assert events == []

        worker = await dispatcher._start_canonical_repeat_time_pin_forward_autodelete_worker_if_enabled(
            repeat_continuation_available=True,
            repeat_time_pin_available=True,
            repeat_time_forward_available=True,
        )
        assert worker is not None
        assert worker.repeat_time_pin_forward_available is True
        assert events == ["construct", "start"]

    asyncio.run(run())
