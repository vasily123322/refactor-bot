import asyncio
from types import SimpleNamespace

from app.bot import dispatcher


def test_required_background_workers_imported() -> None:
    assert dispatcher.CanonicalRepeatContinuationWorker is not None
    assert dispatcher.CanonicalPublicationDeliveryRecoveryWorker is not None
    assert dispatcher.PublicationAutodeleteWorker is not None
    assert dispatcher.PublicationAutodeleteViewsPinForwardWorker is not None
    assert dispatcher.SourceIngestionWorker is not None
    assert dispatcher.LocalCandidateEnrichmentWorker is not None
    assert dispatcher.GrabPoller is not None
    assert dispatcher.AIAutoTasksWorker is not None
    assert dispatcher.StudioServer is not None


def test_run_bot_startup_shutdown_smoke(monkeypatch) -> None:
    events: list[str] = []

    class _FakeEngine:
        sync_engine = SimpleNamespace(pool=object())

        async def dispose(self):
            events.append("engine-dispose")

    class _FakeDispatcher:
        def include_router(self, router):
            events.append("router-include")

        def resolve_used_update_types(self):
            return ["message", "callback_query"]

        async def start_polling(self, *args, **kwargs):
            events.append("polling")

    class _Worker:
        def __init__(self, name: str, **capabilities: bool):
            self.name = name
            for key, value in capabilities.items():
                setattr(self, key, value)

        async def start(self):
            events.append(f"{self.name}-start")

        async def stop(self):
            events.append(f"{self.name}-stop")

    def _worker_class(name: str, **capabilities: bool):
        class _ConcreteWorker(_Worker):
            def __init__(self, *args, **kwargs):
                super().__init__(name, **capabilities)

        return _ConcreteWorker

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

    async def _create_dispatcher():
        return _FakeDispatcher()

    async def _bootstrap_database_schema(*args, **kwargs):
        events.append("schema-bootstrap")
        return SimpleNamespace(managed=True, current_heads=("head",))

    async def _register_commands(bot):
        events.append("commands-register")
        return True

    async def _warm_up_userbot_peers():
        events.append("userbot-warmup")

    async def _cancel_bg_tasks():
        events.append("bg-cancel")

    async def _close_http_clients():
        events.append("http-close")

    async def _started(name: str, **capabilities: bool):
        worker = _Worker(name, **capabilities)
        await worker.start()
        return worker

    async def _start_repeat_continuation():
        return await _started("repeat-continuation")

    async def _start_publication_autodelete():
        return await _started("publication-autodelete")

    async def _start_repeat_time(*, repeat_continuation_available: bool):
        assert repeat_continuation_available is True
        return await _started("repeat-time", repeat_time_available=True)

    async def _start_repeat_time_pin(
        *,
        repeat_continuation_available: bool,
        repeat_time_available: bool,
    ):
        assert repeat_continuation_available is True
        assert repeat_time_available is True
        return await _started("repeat-time-pin", repeat_time_pin_available=True)

    async def _start_repeat_time_forward(
        *,
        repeat_continuation_available: bool,
        repeat_time_available: bool,
    ):
        assert repeat_continuation_available is True
        assert repeat_time_available is True
        return await _started("repeat-time-forward", repeat_time_forward_available=True)

    async def _start_repeat_time_pin_forward(
        *,
        repeat_continuation_available: bool,
        repeat_time_pin_available: bool,
        repeat_time_forward_available: bool,
    ):
        assert repeat_continuation_available is True
        assert repeat_time_pin_available is True
        assert repeat_time_forward_available is True
        return await _started(
            "repeat-time-pin-forward",
            repeat_time_pin_forward_available=True,
        )

    async def _start_views(
        *,
        userbot_available: bool,
        repeat_continuation_available: bool,
    ):
        assert userbot_available is True
        assert repeat_continuation_available is True
        return await _started(
            "publication-views",
            repeat_views_available=True,
            repeat_views_pin_available=True,
            repeat_views_forward_available=True,
            repeat_views_pin_forward_available=True,
        )

    async def _start_delivery(primary_config, **capabilities):
        assert primary_config is not None
        assert all(capabilities.values())
        events.append("delivery-start")
        return object(), object()

    async def _stop_delivery(*, primary_worker, recovery_worker):
        assert primary_worker is not None
        assert recovery_worker is not None
        events.append("delivery-stop")

    monkeypatch.setattr(dispatcher, "settings", SimpleNamespace(
        log_level="INFO",
        sqla_staticpool=False,
        sqla_nullpool=False,
        local_enrichment_worker_enabled=True,
        local_enrichment_worker_interval_seconds=1,
        local_enrichment_worker_batch_size=1,
        local_enrichment_worker_candidate_timeout_seconds=1,
        get_ai_models_config=lambda: {},
    ))
    monkeypatch.setattr(dispatcher, "setup_logging", lambda level: None)
    monkeypatch.setattr(
        dispatcher,
        "load_canonical_publication_delivery_primary_settings",
        lambda: object(),
    )
    monkeypatch.setattr(dispatcher, "validate_runtime_configuration", lambda settings: None)
    monkeypatch.setattr(dispatcher, "prepare_db_storage_sync", lambda: events.append("db-prepare"))
    monkeypatch.setattr(dispatcher, "engine", _FakeEngine())
    monkeypatch.setattr(dispatcher, "bootstrap_database_schema", _bootstrap_database_schema)
    monkeypatch.setattr(dispatcher, "create_dispatcher", _create_dispatcher)
    monkeypatch.setattr(dispatcher, "register_bot_commands", _register_commands)
    monkeypatch.setattr(dispatcher, "ExternalBotsManager", _FakeExternalBotsManager)
    monkeypatch.setattr(dispatcher, "userbot", _FakeUserbot())
    monkeypatch.setattr(dispatcher, "_warm_up_userbot_peers", _warm_up_userbot_peers)
    monkeypatch.setattr(
        dispatcher,
        "_start_canonical_repeat_continuation_worker_if_enabled",
        _start_repeat_continuation,
    )
    monkeypatch.setattr(
        dispatcher,
        "_start_publication_autodelete_worker_if_enabled",
        _start_publication_autodelete,
    )
    monkeypatch.setattr(
        dispatcher,
        "_start_canonical_repeat_time_autodelete_worker_if_enabled",
        _start_repeat_time,
    )
    monkeypatch.setattr(
        dispatcher,
        "_start_canonical_repeat_time_pin_autodelete_worker_if_enabled",
        _start_repeat_time_pin,
    )
    monkeypatch.setattr(
        dispatcher,
        "_start_canonical_repeat_time_forward_autodelete_worker_if_enabled",
        _start_repeat_time_forward,
    )
    monkeypatch.setattr(
        dispatcher,
        "_start_canonical_repeat_time_pin_forward_autodelete_worker_if_enabled",
        _start_repeat_time_pin_forward,
    )
    monkeypatch.setattr(
        dispatcher,
        "_start_publication_autodelete_views_worker_if_enabled",
        _start_views,
    )
    monkeypatch.setattr(
        dispatcher,
        "_start_canonical_publication_delivery_workers",
        _start_delivery,
    )
    monkeypatch.setattr(
        dispatcher,
        "stop_canonical_publication_delivery_workers",
        _stop_delivery,
    )
    monkeypatch.setattr(
        dispatcher,
        "SourceIngestionWorker",
        _worker_class("source-ingestion"),
    )
    monkeypatch.setattr(
        dispatcher,
        "LocalCandidateEnrichmentWorker",
        _worker_class("local-enrichment"),
    )
    monkeypatch.setattr(dispatcher, "GrabPoller", _worker_class("grab-poller"))
    monkeypatch.setattr(dispatcher, "AIAutoTasksWorker", _worker_class("ai-auto"))
    monkeypatch.setattr(dispatcher, "StudioServer", _worker_class("studio"))
    monkeypatch.setattr(dispatcher, "cancel_bg_tasks", _cancel_bg_tasks)
    monkeypatch.setattr(
        dispatcher.OpenRouterClient,
        "close_shared_http_clients",
        staticmethod(_close_http_clients),
    )

    asyncio.run(dispatcher.run_bot())

    required = {
        "db-prepare",
        "schema-bootstrap",
        "router-include",
        "commands-register",
        "external-start",
        "userbot-start",
        "userbot-warmup",
        "repeat-continuation-start",
        "publication-autodelete-start",
        "repeat-time-start",
        "repeat-time-pin-start",
        "repeat-time-forward-start",
        "repeat-time-pin-forward-start",
        "publication-views-start",
        "delivery-start",
        "source-ingestion-start",
        "local-enrichment-start",
        "grab-poller-start",
        "ai-auto-start",
        "studio-start",
        "polling",
        "studio-stop",
        "ai-auto-stop",
        "grab-poller-stop",
        "local-enrichment-stop",
        "source-ingestion-stop",
        "delivery-stop",
        "publication-views-stop",
        "repeat-time-pin-forward-stop",
        "repeat-time-forward-stop",
        "repeat-time-pin-stop",
        "repeat-time-stop",
        "publication-autodelete-stop",
        "repeat-continuation-stop",
        "bg-cancel",
        "external-stop",
        "http-close",
        "engine-dispose",
        "userbot-stop",
    }
    assert required.issubset(events)

    assert events.index("commands-register") < events.index("polling")
    assert events.index("repeat-continuation-start") < events.index("repeat-time-start")
    assert events.index("repeat-time-start") < events.index("repeat-time-pin-start")
    assert events.index("repeat-time-start") < events.index("repeat-time-forward-start")
    assert events.index("repeat-time-pin-start") < events.index("repeat-time-pin-forward-start")
    assert events.index("repeat-time-forward-start") < events.index("repeat-time-pin-forward-start")
    assert events.index("publication-views-start") < events.index("delivery-start")
    assert events.index("delivery-start") < events.index("source-ingestion-start")
    assert events.index("studio-start") < events.index("polling")

    shutdown_order = [
        "studio-stop",
        "ai-auto-stop",
        "grab-poller-stop",
        "local-enrichment-stop",
        "source-ingestion-stop",
        "delivery-stop",
        "publication-views-stop",
        "repeat-time-pin-forward-stop",
        "repeat-time-forward-stop",
        "repeat-time-pin-stop",
        "repeat-time-stop",
        "publication-autodelete-stop",
        "repeat-continuation-stop",
        "bg-cancel",
        "external-stop",
        "http-close",
        "engine-dispose",
        "userbot-stop",
    ]
    positions = [events.index(name) for name in shutdown_order]
    assert positions == sorted(positions)
