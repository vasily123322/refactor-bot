import asyncio

import pytest

from app.api.studio import server as server_module
from app.api.studio.config import StudioConfig
from app.bot import dispatcher


def _config(*, enabled: bool = True) -> StudioConfig:
    return StudioConfig(
        enabled=enabled,
        host="127.0.0.1",
        port=8080,
        public_url=None,
        init_data_max_age_seconds=86400,
        cors_origins=(),
    )


def _patch_uvicorn(monkeypatch, server_cls) -> None:
    monkeypatch.setattr(server_module, "create_studio_app", lambda config: object())
    monkeypatch.setattr(server_module.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(server_module.uvicorn, "Server", server_cls)


def test_studio_start_reports_failure_before_readiness(monkeypatch) -> None:
    class _FailingServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self):
            raise OSError("address already in use")

    _patch_uvicorn(monkeypatch, _FailingServer)

    async def run() -> None:
        server = server_module.StudioServer(_config(), startup_timeout_seconds=0.1)
        with pytest.raises(RuntimeError, match="before readiness") as exc_info:
            await server.start()
        assert isinstance(exc_info.value.__cause__, OSError)
        assert server.ready is False

    asyncio.run(run())


def test_studio_reports_later_server_task_failure(monkeypatch) -> None:
    gate: asyncio.Event | None = None

    class _LaterFailingServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self):
            nonlocal gate
            self.started = True
            gate = asyncio.Event()
            await gate.wait()
            raise RuntimeError("server crashed")

    _patch_uvicorn(monkeypatch, _LaterFailingServer)

    async def run() -> None:
        server = server_module.StudioServer(_config(), startup_timeout_seconds=0.1)
        await server.start()
        assert server.ready is True
        assert gate is not None
        wait_task = asyncio.create_task(server.wait_for_termination())
        gate.set()
        with pytest.raises(RuntimeError, match="failed after readiness") as exc_info:
            await wait_task
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        await server.stop()
        assert server.ready is False

    asyncio.run(run())


def test_studio_clean_shutdown_is_not_reported_as_runtime_failure(monkeypatch) -> None:
    class _RunningServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self):
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)

    _patch_uvicorn(monkeypatch, _RunningServer)

    async def run() -> None:
        server = server_module.StudioServer(_config(), startup_timeout_seconds=0.1)
        await server.start()
        watcher = asyncio.create_task(server.wait_for_termination())
        await server.stop()
        await watcher
        assert server.ready is False

    asyncio.run(run())


def test_bot_polling_is_cancelled_when_studio_terminates() -> None:
    class _FakeDispatcher:
        cancelled = False

        def resolve_used_update_types(self):
            return ["message"]

        async def start_polling(self, *args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                self.cancelled = True

    class _FailingStudio:
        enabled = True

        async def wait_for_termination(self):
            await asyncio.sleep(0)
            raise RuntimeError("studio unavailable")

    async def run() -> None:
        dp = _FakeDispatcher()
        with pytest.raises(RuntimeError, match="studio unavailable"):
            await dispatcher._run_polling_with_studio_supervision(dp, _FailingStudio())
        assert dp.cancelled is True

    asyncio.run(run())
