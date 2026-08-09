import asyncio
from types import SimpleNamespace

from app.bot import dispatcher


def test_required_background_workers_imported() -> None:
    assert dispatcher.AIAutoTasksWorker is not None
    assert dispatcher.SourceIngestionWorker is not None
    assert dispatcher.SchedulerRecoveryWorker is not None


def test_run_bot_startup_shutdown_smoke(monkeypatch) -> None:
    events: list[str] = []

    class _FakeConnection:
        async def run_sync(self, fn):
            events.append("db-create")

    class _BeginContext:
        async def __aenter__(self):
            return _FakeConnection()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeEngine:
        def begin(self):
            return _BeginContext()

        async def dispose(self):
            events.append("engine-dispose")

    class _FakeDispatcher:
        def include_router(self, router):
            events.append("router-include")

        def resolve_used_update_types(self):
            return ["message", "callback_query"]

        async def start_polling(self, *args, **kwargs):
            events.append("polling")

    class _FakeExternalBotsManager:
        async def start_all(self):
            events.append("external-start")

        async def stop_all(self):
            events.append("external-stop")

    class _FakeUserbot:
        async def start(self):
            events.append("userbot-start")

        async def stop(self):
            events.append("userbot-stop")

        async def get_dialogs(self, limit=200):
            if False:
                yield None

    def _worker_class(name: str):
        class _Worker:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                events.append(f"{name}-start")

            async def stop(self):
                events.append(f"{name}-stop")

        return _Worker

    async def _create_dispatcher():
        return _FakeDispatcher()

    async def _cancel_bg_tasks():
        events.append("bg-cancel")

    async def _register_commands(bot):
        events.append("commands-register")
        return True

    async def _legacy_schema_boundary(engine, *, unmanaged_initializer):
        await unmanaged_initializer()
        return SimpleNamespace(managed=False, current_heads=())

    monkeypatch.setattr(dispatcher, "engine", _FakeEngine())
    monkeypatch.setattr(dispatcher, "create_dispatcher", _create_dispatcher)
    monkeypatch.setattr(dispatcher, "ExternalBotsManager", _FakeExternalBotsManager)
    monkeypatch.setattr(dispatcher, "userbot", _FakeUserbot())
    monkeypatch.setattr(dispatcher, "Scheduler", _worker_class("scheduler"))
    monkeypatch.setattr(
        dispatcher,
        "SchedulerRecoveryWorker",
        _worker_class("scheduler-recovery"),
    )
    monkeypatch.setattr(
        dispatcher,
        "PublicationReconcilerWorker",
        _worker_class("publication-reconciler"),
    )
    monkeypatch.setattr(
        dispatcher,
        "SourceIngestionWorker",
        _worker_class("source-ingestion"),
    )
    monkeypatch.setattr(dispatcher, "GrabPoller", _worker_class("grab-poller"))
    monkeypatch.setattr(dispatcher, "AIAutoTasksWorker", _worker_class("ai-auto"))
    monkeypatch.setattr(dispatcher, "PostingService", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher, "register_bot_commands", _register_commands)
    monkeypatch.setattr(
        dispatcher, "init_db_if_needed_sync", lambda: events.append("db-sync")
    )
    monkeypatch.setattr(dispatcher, "prepare_db_storage_sync", lambda: None)
    monkeypatch.setattr(
        dispatcher,
        "bootstrap_database_schema",
        _legacy_schema_boundary,
    )
    monkeypatch.setattr(dispatcher, "cancel_bg_tasks", _cancel_bg_tasks)
    monkeypatch.setattr(
        type(dispatcher.settings), "get_ai_models_config", lambda self: {}
    )

    asyncio.run(dispatcher.run_bot())

    required = {
        "db-sync",
        "db-create",
        "router-include",
        "commands-register",
        "external-start",
        "userbot-start",
        "scheduler-start",
        "scheduler-recovery-start",
        "publication-reconciler-start",
        "source-ingestion-start",
        "grab-poller-start",
        "ai-auto-start",
        "polling",
        "ai-auto-stop",
        "grab-poller-stop",
        "source-ingestion-stop",
        "publication-reconciler-stop",
        "scheduler-recovery-stop",
        "scheduler-stop",
        "bg-cancel",
        "external-stop",
        "engine-dispose",
        "userbot-stop",
    }
    assert required.issubset(events)

    assert events.index("commands-register") < events.index("polling")
    assert events.index("scheduler-start") < events.index("scheduler-stop")
    assert events.index("scheduler-start") < events.index("scheduler-recovery-start")
    assert events.index("scheduler-recovery-start") < events.index("scheduler-recovery-stop")
    assert events.index("scheduler-recovery-stop") < events.index("scheduler-stop")
    assert events.index("publication-reconciler-start") < events.index("publication-reconciler-stop")
    assert events.index("source-ingestion-start") < events.index("source-ingestion-stop")
    assert events.index("grab-poller-start") < events.index("grab-poller-stop")
    assert events.index("ai-auto-start") < events.index("ai-auto-stop")
    assert events.index("external-start") < events.index("external-stop")
    assert events.index("userbot-start") < events.index("userbot-stop")
