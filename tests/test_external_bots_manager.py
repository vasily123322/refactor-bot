import asyncio
from types import SimpleNamespace

from app.services import external_bots as external_bots_module


def test_polling_failure_restarts_with_replacement_and_shutdown_stops_restart(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        real_sleep = asyncio.sleep
        first_failure = asyncio.Event()
        replacement_started = asyncio.Event()
        second_failure = asyncio.Event()
        second_backoff_started = asyncio.Event()

        storages = []
        bots = []
        dispatchers = []
        restart_sleeps = 0

        class FakeStorage:
            def __init__(self) -> None:
                self.closed = False
                storages.append(self)

            async def close(self) -> None:
                self.closed = True

        class FakeBotSession:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        class FakeBot:
            def __init__(self, token, default=None) -> None:
                self.token = token
                self.default = default
                self.session = FakeBotSession()
                bots.append(self)

        class FakeDispatcher:
            def __init__(self, storage) -> None:
                self.storage = storage
                self.index = len(dispatchers)
                dispatchers.append(self)

            def include_router(self, router) -> None:
                self.router = router

            def resolve_used_update_types(self):
                return ["message"]

            async def start_polling(self, *args, **kwargs) -> None:
                if self.index == 0:
                    await first_failure.wait()
                    raise RuntimeError("forced polling failure")
                if self.index == 1:
                    replacement_started.set()
                    await second_failure.wait()
                    raise RuntimeError("forced replacement failure")
                raise AssertionError("unexpected extra polling task")

        class FakeSessionContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeRepo:
            def __init__(self, session) -> None:
                self.session = session

            async def get_by_id(self, ext_id):
                return SimpleNamespace(id=ext_id, is_active=True, token="token")

        async def fake_sleep(delay) -> None:
            nonlocal restart_sleeps
            if delay == 30:
                await real_sleep(3600)
                return
            restart_sleeps += 1
            if restart_sleeps == 1:
                await real_sleep(0)
                return
            second_backoff_started.set()
            await real_sleep(3600)

        monkeypatch.setattr(external_bots_module, "Bot", FakeBot)
        monkeypatch.setattr(external_bots_module, "Dispatcher", FakeDispatcher)
        monkeypatch.setattr(external_bots_module, "build_fsm_storage", FakeStorage)
        monkeypatch.setattr(
            external_bots_module,
            "build_router_for_external_bot",
            lambda ext_id: object(),
        )
        monkeypatch.setattr(
            external_bots_module,
            "AsyncSessionLocal",
            lambda: FakeSessionContext(),
        )
        monkeypatch.setattr(external_bots_module, "ExternalBotsRepo", FakeRepo)
        monkeypatch.setattr(external_bots_module.asyncio, "sleep", fake_sleep)

        manager = external_bots_module.ExternalBotsManager()
        await manager.start_one(7, "token")
        first_task = manager._tasks[7]
        first_approver = manager._approver_tasks[7]
        first_bot = bots[0]
        first_storage = storages[0]

        first_failure.set()
        await asyncio.wait_for(replacement_started.wait(), timeout=1)

        replacement_task = manager._tasks[7]
        assert replacement_task is not first_task
        assert not replacement_task.done()
        assert first_task.done()
        assert first_approver.done()
        assert first_bot.session.closed
        assert first_storage.closed

        second_failure.set()
        await asyncio.wait_for(second_backoff_started.wait(), timeout=1)
        await manager.stop_all()
        await manager.stop_all()
        await real_sleep(0)

        assert len(dispatchers) == 2
        assert manager._tasks == {}
        assert manager._dps == {}
        assert manager._bots == {}
        assert manager._approver_tasks == {}
        assert bots[1].session.closed
        assert storages[1].closed

    asyncio.run(scenario())
