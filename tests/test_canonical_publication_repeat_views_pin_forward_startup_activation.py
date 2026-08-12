from __future__ import annotations

import asyncio

import pytest

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)


def _config() -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(
        _env_file=None,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
    )


def test_dispatcher_combined_worker_publishes_exact_fact_only_after_successful_start(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )
        constructed: list[dict[str, bool]] = []

        class Worker:
            def __init__(self, **kwargs) -> None:
                self.allow_repeat_views = bool(kwargs["allow_repeat_views"])
                self.allow_repeat_views_pin = bool(kwargs["allow_repeat_views_pin"])
                self.allow_repeat_views_forward = bool(
                    kwargs["allow_repeat_views_forward"]
                )
                self.allow_repeat_views_pin_forward = bool(
                    kwargs["allow_repeat_views_pin_forward"]
                )
                self._started = False
                constructed.append(
                    {
                        "repeat": self.allow_repeat_views,
                        "pin": self.allow_repeat_views_pin,
                        "forward": self.allow_repeat_views_forward,
                        "combined": self.allow_repeat_views_pin_forward,
                    }
                )

            @property
            def repeat_views_available(self) -> bool:
                return bool(self._started and self.allow_repeat_views)

            @property
            def repeat_views_pin_available(self) -> bool:
                return bool(
                    self._started
                    and self.allow_repeat_views
                    and self.allow_repeat_views_pin
                )

            @property
            def repeat_views_forward_available(self) -> bool:
                return bool(
                    self._started
                    and self.allow_repeat_views
                    and self.allow_repeat_views_forward
                )

            @property
            def repeat_views_pin_forward_available(self) -> bool:
                return bool(
                    self._started
                    and self.allow_repeat_views
                    and self.allow_repeat_views_pin
                    and self.allow_repeat_views_forward
                    and self.allow_repeat_views_pin_forward
                )

            async def start(self) -> None:
                self._started = True

            async def stop(self) -> None:
                self._started = False

        monkeypatch.setattr(
            dispatcher,
            "PublicationAutodeleteViewsPinForwardWorker",
            Worker,
        )

        closed = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
            repeat_continuation_available=False,
        )
        assert closed is not None
        assert closed.repeat_views_available is False
        assert closed.repeat_views_pin_available is False
        assert closed.repeat_views_forward_available is False
        assert closed.repeat_views_pin_forward_available is False

        opened = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
            repeat_continuation_available=True,
        )
        assert opened is not None
        assert opened.repeat_views_available is True
        assert opened.repeat_views_pin_available is True
        assert opened.repeat_views_forward_available is True
        assert opened.repeat_views_pin_forward_available is True
        assert constructed == [
            {"repeat": False, "pin": True, "forward": True, "combined": True},
            {"repeat": True, "pin": True, "forward": True, "combined": True},
        ]

    asyncio.run(run())


def test_combined_worker_start_failure_is_cleaned_before_primary_activation(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )
        stopped: list[bool] = []

        class FailingWorker:
            def __init__(self, **kwargs) -> None:
                assert kwargs["allow_repeat_views_pin_forward"] is True

            async def start(self) -> None:
                raise RuntimeError("combined startup failed")

            async def stop(self) -> None:
                stopped.append(True)

        monkeypatch.setattr(
            dispatcher,
            "PublicationAutodeleteViewsPinForwardWorker",
            FailingWorker,
        )

        with pytest.raises(RuntimeError, match="startup failed"):
            await dispatcher._start_publication_autodelete_views_worker_if_enabled(
                userbot_available=True,
                repeat_continuation_available=True,
            )
        assert stopped == [True]

    asyncio.run(run())


def test_primary_startup_forwards_combined_started_fact_independently(monkeypatch) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        recovery = object()
        captured: list[dict[str, bool]] = []

        async def start_recovery():
            return recovery

        async def start_primary(**kwargs):
            captured.append(
                {
                    "repeat": bool(kwargs["repeat_views_executor_available"]),
                    "pin": bool(kwargs["repeat_views_pin_executor_available"]),
                    "forward": bool(kwargs["repeat_views_forward_executor_available"]),
                    "combined": bool(
                        kwargs["repeat_views_pin_forward_executor_available"]
                    ),
                }
            )
            return object()

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
            repeat_views_executor_available=True,
            repeat_views_pin_executor_available=True,
            repeat_views_forward_executor_available=True,
            repeat_views_pin_forward_executor_available=False,
        )
        await dispatcher._start_canonical_publication_delivery_workers(
            _config(),
            views_autodelete_executor_available=True,
            repeat_continuation_executor_available=True,
            repeat_views_executor_available=True,
            repeat_views_pin_executor_available=True,
            repeat_views_forward_executor_available=True,
            repeat_views_pin_forward_executor_available=True,
        )
        assert captured == [
            {"repeat": True, "pin": True, "forward": True, "combined": False},
            {"repeat": True, "pin": True, "forward": True, "combined": True},
        ]

    asyncio.run(run())
