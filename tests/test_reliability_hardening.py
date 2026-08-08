import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import dispatcher
from app.core.runner import PollingLoop
from app.workers import grab_poll


def test_run_bot_cleans_up_after_partial_worker_start(monkeypatch) -> None:
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
        sync_engine = None

        def begin(self):
            return _BeginContext()

        async def dispose(self):
            events.append("engine-dispose")

    class _FakeDispatcher:
        def include_router(self, router):
            events.append("router-include")

        def resolve_used_update_types(self):
            return ["message"]

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

    class _FailingScheduler:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            events.append("scheduler-start")
            raise RuntimeError("scheduler boom")

        async def stop(self):
            events.append("scheduler-stop")

    async def _create_dispatcher():
        return _FakeDispatcher()

    async def _cancel_bg_tasks():
        events.append("bg-cancel")

    monkeypatch.setattr(dispatcher, "engine", _FakeEngine())
    monkeypatch.setattr(dispatcher, "create_dispatcher", _create_dispatcher)
    monkeypatch.setattr(dispatcher, "ExternalBotsManager", _FakeExternalBotsManager)
    monkeypatch.setattr(dispatcher, "userbot", _FakeUserbot())
    monkeypatch.setattr(dispatcher, "Scheduler", _FailingScheduler)
    monkeypatch.setattr(dispatcher, "PostingService", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher, "init_db_if_needed_sync", lambda: None)
    monkeypatch.setattr(dispatcher, "cancel_bg_tasks", _cancel_bg_tasks)
    monkeypatch.setattr(
        type(dispatcher.settings), "get_ai_models_config", lambda self: {}
    )

    with pytest.raises(RuntimeError, match="scheduler boom"):
        asyncio.run(dispatcher.run_bot())

    assert "scheduler-stop" in events
    assert "bg-cancel" in events
    assert "external-stop" in events
    assert "engine-dispose" in events
    assert "userbot-stop" in events
    assert "polling" not in events


def test_polling_loop_continues_after_tick_failure() -> None:
    calls = 0
    loop = None

    async def _tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        loop._stopping.set()

    loop = PollingLoop(interval_seconds=0, on_tick=_tick, name="test-loop")
    asyncio.run(loop._run_loop())

    assert calls == 2


def test_grab_poller_processes_new_text_message(monkeypatch) -> None:
    source = SimpleNamespace(
        id=7,
        source_value="@source_channel",
        mode="summary",
        channel_id=42,
        citation_enabled=False,
    )
    message = SimpleNamespace(id=11, text="new post", caption=None)
    ai_settings = SimpleNamespace(
        custom_prompt=None,
        preset_id=None,
        user_prompt_template=None,
        model="test/model",
    )
    target_channel = SimpleNamespace(tg_chat_id=-1001234567890)
    sent: list[tuple[int, dict]] = []

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeAIRepo:
        def __init__(self, session):
            pass

        async def get_or_create(self, channel_id):
            return ai_settings

    class _FakeGenerationService:
        def __init__(self, session):
            pass

        async def run_pipeline(self, **kwargs):
            assert kwargs["original_text"] == "new post"
            return {"success": True, "text": "generated", "error": None}

    class _FakeSettingsRepo:
        def __init__(self, session):
            pass

        async def get_by_channel_id(self, channel_id):
            return None

    class _FakeChannelsRepo:
        def __init__(self, session):
            pass

        async def get_by_id(self, channel_id):
            return target_channel

    class _FakePostingService:
        def __init__(self, *args, **kwargs):
            pass

        async def send_now(self, *, channel_id, payload):
            sent.append((channel_id, payload))
            return [101]

    poller = grab_poll.GrabPoller(interval_seconds=30)
    poller._last_processed[source.id] = 10

    monkeypatch.setattr(grab_poll, "AsyncSessionLocal", lambda: _SessionContext())
    monkeypatch.setattr(grab_poll, "ChannelAISettingsRepo", _FakeAIRepo)
    monkeypatch.setattr(grab_poll, "AIGenerationService", _FakeGenerationService)
    monkeypatch.setattr(grab_poll, "ChannelSettingsRepo", _FakeSettingsRepo)
    monkeypatch.setattr(grab_poll, "ChannelsRepo", _FakeChannelsRepo)
    monkeypatch.setattr(grab_poll, "PostingService", _FakePostingService)
    monkeypatch.setattr(poller, "_try_join", AsyncMock())
    monkeypatch.setattr(poller, "_fetch_latest_message", AsyncMock(return_value=message))
    monkeypatch.setattr(
        poller, "_get_channel_owner_user_id", AsyncMock(return_value=12345)
    )
    monkeypatch.setattr(
        poller, "_append_citation", AsyncMock(return_value="generated")
    )

    asyncio.run(poller._process_source(source))

    assert sent == [
        (
            target_channel.tg_chat_id,
            {"type": "text", "text": "generated", "silent": True},
        )
    ]
    assert poller._last_processed[source.id] == message.id
