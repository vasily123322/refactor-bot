from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle
from app.workers import canonical_repeat_time_pin_autodelete as worker_module
from app.workers.canonical_repeat_time_autodelete import (
    CanonicalRepeatTimeAutodeleteWorker,
)
from app.workers.canonical_repeat_time_pin_autodelete import (
    CanonicalRepeatTimePinAutodeleteWorker,
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


def test_repeat_time_pin_availability_is_started_only_and_default_off() -> None:
    async def run() -> None:
        enabled = CanonicalRepeatTimePinAutodeleteWorker(
            provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
            allow_repeat_time_pin=True,
        )
        loop = _Loop()
        enabled._loop = loop  # type: ignore[assignment]
        assert enabled.repeat_time_pin_available is False
        assert not hasattr(enabled, "repeat_time_available")
        await enabled.start()
        assert enabled.repeat_time_pin_available is True
        await enabled.stop()
        assert enabled.repeat_time_pin_available is False
        assert loop.start_calls == 1
        assert loop.stop_calls == 1

        default_off = CanonicalRepeatTimePinAutodeleteWorker(
            provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
        )
        default_loop = _Loop()
        default_off._loop = default_loop  # type: ignore[assignment]
        await default_off.start()
        assert default_off.allow_repeat_time_pin is False
        assert default_off.repeat_time_pin_available is False
        await default_off.stop()

        failed = CanonicalRepeatTimePinAutodeleteWorker(
            provider=object(),
            session_factory=object(),  # type: ignore[arg-type]
            allow_repeat_time_pin=True,
        )
        failed._loop = _Loop(fail=True)  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="loop start failed"):
            await failed.start()
        assert failed.repeat_time_pin_available is False

    asyncio.run(run())


def test_plain_repeat_time_worker_has_no_time_pin_availability_fact() -> None:
    worker = CanonicalRepeatTimeAutodeleteWorker(
        provider=object(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_repeat_time=True,
    )
    assert worker.repeat_time_available is False
    assert not hasattr(worker, "repeat_time_pin_available")
    assert not hasattr(worker, "allow_repeat_time_pin")


def test_time_pin_worker_passes_exact_mode_and_lease_to_composition_service(
    monkeypatch,
) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}
        handle = PublicationAutodeleteLeaseHandle(
            publication_id=19,
            lease_token="repeat-time-pin-lease",
            holder="repeat-time-pin-worker",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )

        class FakeService:
            def __init__(self, session, **kwargs) -> None:
                captured["session"] = session
                captured["kwargs"] = kwargs

            async def delete_if_due(self, publication_id: int, *, lease):
                captured["publication_id"] = int(publication_id)
                captured["lease"] = lease
                return SimpleNamespace(outcome="ineligible")

        monkeypatch.setattr(
            worker_module,
            "CanonicalRepeatTimePinAutodeleteService",
            FakeService,
        )

        worker = CanonicalRepeatTimePinAutodeleteWorker(
            provider=object(),
            session_factory=_SessionFactory(),  # type: ignore[arg-type]
            allow_repeat_time_pin=True,
        )

        async def select():
            return SimpleNamespace(publication_ids=(19,), done=True, next_cursor=19)

        async def acquire(publication_id: int):
            assert publication_id == 19
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
        assert captured["publication_id"] == 19
        assert captured["lease"] is handle
        kwargs = captured["kwargs"]
        assert kwargs["allow_repeat_time_pin"] is True  # type: ignore[index]
        assert kwargs["allow_report"] is True  # type: ignore[index]

    asyncio.run(run())
