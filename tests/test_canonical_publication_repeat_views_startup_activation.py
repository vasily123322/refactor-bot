from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)


def _config() -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(
        _env_file=None,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
    )


def test_views_worker_repeat_authority_is_derived_from_started_continuation_fact(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )
        seen: list[bool] = []

        class Worker:
            def __init__(self, **kwargs) -> None:
                self.allow_repeat_views = bool(kwargs["allow_repeat_views"])
                seen.append(self.allow_repeat_views)

            async def start(self) -> None:
                return None

        monkeypatch.setattr(dispatcher, "PublicationAutodeleteViewsWorker", Worker)

        closed = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
            repeat_continuation_available=False,
        )
        assert closed is not None
        assert closed.allow_repeat_views is False

        opened = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
            repeat_continuation_available=True,
        )
        assert opened is not None
        assert opened.allow_repeat_views is True
        assert seen == [False, True]

    asyncio.run(run())


def test_views_worker_never_constructs_from_config_when_userbot_dependency_missing(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )

        class Unexpected:
            def __init__(self, **kwargs) -> None:
                raise AssertionError("config alone must not construct repeat views worker")

        monkeypatch.setattr(dispatcher, "PublicationAutodeleteViewsWorker", Unexpected)
        assert (
            await dispatcher._start_publication_autodelete_views_worker_if_enabled(
                userbot_available=False,
                repeat_continuation_available=True,
            )
            is None
        )

    asyncio.run(run())


def test_dispatcher_forwards_only_explicit_repeat_views_worker_fact_to_primary(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        recovery = object()
        primary = object()
        captured: list[bool] = []

        async def start_recovery():
            return recovery

        async def start_primary(**kwargs):
            captured.append(bool(kwargs["repeat_views_executor_available"]))
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

        await dispatcher._start_canonical_publication_delivery_workers(
            _config(),
            views_autodelete_executor_available=True,
            repeat_continuation_executor_available=True,
            repeat_views_executor_available=False,
        )
        await dispatcher._start_canonical_publication_delivery_workers(
            _config(),
            views_autodelete_executor_available=True,
            repeat_continuation_executor_available=True,
            repeat_views_executor_available=True,
        )
        assert captured == [False, True]

    asyncio.run(run())


def test_safe_repeat_runtime_control_requires_all_runtime_dependencies_for_composition(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.services import canonical_publication_safe_repeat_runtime_control as control

        captured: list[dict[str, object]] = []

        def build_runtime(**kwargs):
            captured.append(dict(kwargs))
            return SimpleNamespace(executor=object())

        class Handoff:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

        class Worker:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

        monkeypatch.setattr(
            control,
            "build_canonical_publication_safe_repeat_runtime",
            build_runtime,
        )
        monkeypatch.setattr(control, "CanonicalPublicationRepeatHandoffExecutor", Handoff)
        monkeypatch.setattr(control, "CanonicalPublicationDeliveryWorker", Worker)

        cases = (
            # composition fact cannot substitute for missing continuation
            dict(
                views_autodelete_executor_available=True,
                repeat_continuation_available=False,
                repeat_views_executor_available=True,
                expected=False,
            ),
            # composition fact cannot substitute for missing views executor
            dict(
                views_autodelete_executor_available=False,
                repeat_continuation_available=True,
                repeat_views_executor_available=True,
                expected=False,
            ),
            # independent dependencies do not compose without explicit fact
            dict(
                views_autodelete_executor_available=True,
                repeat_continuation_available=True,
                repeat_views_executor_available=False,
                expected=False,
            ),
            # only the complete started dependency set opens the composition
            dict(
                views_autodelete_executor_available=True,
                repeat_continuation_available=True,
                repeat_views_executor_available=True,
                expected=True,
            ),
        )
        for case in cases:
            expected = bool(case.pop("expected"))
            worker = await control.start_canonical_publication_safe_repeat_primary_if_enabled(
                config=_config(),
                recovery_worker=object(),
                bot=object(),
                repeat_owner_policy_enforced=True,
                **case,
            )
            assert worker is not None
            assert bool(captured[-1]["allow_repeat_views"]) is expected

    asyncio.run(run())
