from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.services import canonical_publication_delivery_runtime_control as runtime_control
from app.services import canonical_publication_safe_repeat_runtime_control as safe_control
from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_primary_started,
    canonical_publication_delivery_time_autodelete_started,
    set_canonical_publication_delivery_primary_worker,
)


def _enabled_config() -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(
        _env_file=None,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
    )


def test_safe_repeat_primary_publishes_authority_only_after_successful_start(
    monkeypatch,
) -> None:
    async def run() -> None:
        set_canonical_publication_delivery_primary_worker(None)
        events: list[str] = []

        monkeypatch.setattr(
            safe_control,
            "build_canonical_publication_safe_repeat_runtime",
            lambda **kwargs: SimpleNamespace(executor=object()),
        )
        monkeypatch.setattr(
            safe_control,
            "CanonicalPublicationRepeatHandoffExecutor",
            lambda **kwargs: object(),
        )

        class StartedWorker:
            def __init__(self, **kwargs) -> None:
                assert canonical_publication_delivery_primary_started() is False
                assert canonical_publication_delivery_time_autodelete_started() is False

            async def start(self) -> None:
                events.append("start")
                assert canonical_publication_delivery_primary_started() is False
                assert canonical_publication_delivery_time_autodelete_started() is False

            async def stop(self) -> None:
                events.append("stop")

        monkeypatch.setattr(
            safe_control,
            "CanonicalPublicationDeliveryWorker",
            StartedWorker,
        )

        worker = await safe_control.start_canonical_publication_safe_repeat_primary_if_enabled(
            config=_enabled_config(),
            recovery_worker=object(),
            bot=object(),
            session_factory=object(),  # type: ignore[arg-type]
            time_autodelete_executor_available=True,
        )

        assert isinstance(worker, StartedWorker)
        assert events == ["start"]
        assert canonical_publication_delivery_primary_started() is True
        assert canonical_publication_delivery_time_autodelete_started() is True
        set_canonical_publication_delivery_primary_worker(None)

    asyncio.run(run())


def test_safe_repeat_primary_start_failure_never_publishes_authority(monkeypatch) -> None:
    async def run() -> None:
        set_canonical_publication_delivery_primary_worker(None)
        events: list[str] = []

        monkeypatch.setattr(
            safe_control,
            "build_canonical_publication_safe_repeat_runtime",
            lambda **kwargs: SimpleNamespace(executor=object()),
        )
        monkeypatch.setattr(
            safe_control,
            "CanonicalPublicationRepeatHandoffExecutor",
            lambda **kwargs: object(),
        )

        class FailingWorker:
            def __init__(self, **kwargs) -> None:
                pass

            async def start(self) -> None:
                events.append("start")
                raise RuntimeError("primary startup failed")

            async def stop(self) -> None:
                events.append("stop")

        monkeypatch.setattr(
            safe_control,
            "CanonicalPublicationDeliveryWorker",
            FailingWorker,
        )

        with pytest.raises(RuntimeError, match="primary startup failed"):
            await safe_control.start_canonical_publication_safe_repeat_primary_if_enabled(
                config=_enabled_config(),
                recovery_worker=object(),
                bot=object(),
                session_factory=object(),  # type: ignore[arg-type]
                time_autodelete_executor_available=True,
            )

        assert events == ["start", "stop"]
        assert canonical_publication_delivery_primary_started() is False
        assert canonical_publication_delivery_time_autodelete_started() is False

    asyncio.run(run())


def test_common_shutdown_releases_authority_before_primary_stop() -> None:
    async def run() -> None:
        events: list[str] = []

        class Primary:
            async def stop(self) -> None:
                assert canonical_publication_delivery_primary_started() is False
                assert canonical_publication_delivery_time_autodelete_started() is False
                events.append("primary")

        class Recovery:
            async def stop(self) -> None:
                events.append("recovery")

        primary = Primary()
        set_canonical_publication_delivery_primary_worker(
            primary,
            time_autodelete_available=True,
        )
        assert canonical_publication_delivery_primary_started() is True
        assert canonical_publication_delivery_time_autodelete_started() is True

        await runtime_control.stop_canonical_publication_delivery_workers(
            primary_worker=primary,
            recovery_worker=Recovery(),
        )

        assert events == ["primary", "recovery"]
        assert canonical_publication_delivery_primary_started() is False
        assert canonical_publication_delivery_time_autodelete_started() is False

    asyncio.run(run())
