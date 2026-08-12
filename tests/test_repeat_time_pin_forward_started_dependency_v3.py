from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle
from app.workers import canonical_repeat_time_pin_forward_autodelete as worker_module
from app.workers.canonical_repeat_time_forward_autodelete import (
    CanonicalRepeatTimeForwardAutodeleteWorker,
)
from app.workers.canonical_repeat_time_pin_autodelete import (
    CanonicalRepeatTimePinAutodeleteWorker,
)
from app.workers.canonical_repeat_time_pin_forward_autodelete import (
    CanonicalRepeatTimePinForwardAutodeleteWorker,
)


class _Loop:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = bool(fail)
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


def test_combined_availability_is_started_only_and_not_inferred_from_narrower_workers() -> None:
    async def run() -> None:
        worker = CanonicalRepeatTimePinForwardAutodeleteWorker(
            provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
            allow_repeat_time_pin_forward=True,
        )
        loop = _Loop()
        worker._loop = loop  # type: ignore[assignment]
        assert worker.repeat_time_pin_forward_available is False
        await worker.start()
        assert worker.repeat_time_pin_forward_available is True
        await worker.stop()
        assert worker.repeat_time_pin_forward_available is False

        default_off = CanonicalRepeatTimePinForwardAutodeleteWorker(
            provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
        )
        default_off._loop = _Loop()  # type: ignore[assignment]
        await default_off.start()
        assert default_off.repeat_time_pin_forward_available is False
        await default_off.stop()

        failed = CanonicalRepeatTimePinForwardAutodeleteWorker(
            provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
            allow_repeat_time_pin_forward=True,
        )
        failed._loop = _Loop(fail=True)  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="loop start failed"):
            await failed.start()
        assert failed.repeat_time_pin_forward_available is False

        pin = CanonicalRepeatTimePinAutodeleteWorker(
            provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
            allow_repeat_time_pin=True,
        )
        forward = CanonicalRepeatTimeForwardAutodeleteWorker(
            provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
            allow_repeat_time_forward=True,
        )
        assert not hasattr(pin, "repeat_time_pin_forward_available")
        assert not hasattr(forward, "repeat_time_pin_forward_available")

    asyncio.run(run())


def test_combined_worker_passes_exact_mode_and_lease(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}
        handle = PublicationAutodeleteLeaseHandle(
            publication_id=23,
            lease_token="combined-lease",
            holder="combined-worker",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )

        class FakeService:
            def __init__(self, session, **kwargs) -> None:
                captured["kwargs"] = kwargs

            async def delete_if_due(self, publication_id: int, *, lease):
                captured["publication_id"] = publication_id
                captured["lease"] = lease
                return SimpleNamespace(outcome="ineligible")

        monkeypatch.setattr(
            worker_module,
            "CanonicalRepeatTimePinForwardAutodeleteService",
            FakeService,
        )
        worker = CanonicalRepeatTimePinForwardAutodeleteWorker(
            provider=object(),
            session_factory=_SessionFactory(),  # type: ignore[arg-type]
            allow_repeat_time_pin_forward=True,
        )

        async def select():
            return SimpleNamespace(publication_ids=(23,), done=True, next_cursor=23)

        async def acquire(publication_id: int):
            return handle

        async def release(candidate):
            return True

        worker._select = select  # type: ignore[method-assign]
        worker._acquire = acquire  # type: ignore[method-assign]
        worker._release = release  # type: ignore[method-assign]
        tick = await worker.run_once()
        assert tick.ineligible == 1
        assert captured["publication_id"] == 23
        assert captured["lease"] is handle
        kwargs = captured["kwargs"]
        assert kwargs["allow_repeat_time_pin_forward"] is True  # type: ignore[index]
        assert kwargs["allow_report"] is True  # type: ignore[index]

    asyncio.run(run())
