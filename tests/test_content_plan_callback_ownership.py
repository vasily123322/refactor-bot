from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.settings_channel_access import SettingsChannelOwnerMiddleware


class _SessionContext:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSession:
    def __init__(self, *, task_channel_id: int | None = None) -> None:
        self.task_channel_id = task_channel_id

    async def get(self, model, object_id: int):
        from app.domain.models import PostTask

        if model is PostTask and int(object_id) == 44 and self.task_channel_id is not None:
            return SimpleNamespace(channel_id=self.task_channel_id)
        return None


class _FakeEvent:
    def __init__(self, data: str, *, user_id: int = 7001) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def test_foreign_direct_content_plan_channel_is_denied_before_handler(monkeypatch) -> None:
    async def run() -> None:
        from app.core import settings_channel_access as access_module

        monkeypatch.setattr(
            access_module,
            "AsyncSessionLocal",
            lambda: _SessionContext(_FakeSession()),
        )

        async def owns(session, *, tg_user_id: int, channel_id: int) -> bool:
            return int(tg_user_id) == 7001 and int(channel_id) == 12

        monkeypatch.setattr(access_module, "_user_owns_channel", owns)
        handled: list[bool] = []

        async def handler(event, data):
            handled.append(True)
            return "handled"

        event = _FakeEvent("cp_pick_channel_99")
        result = await SettingsChannelOwnerMiddleware()(handler, event, {})

        assert result is None
        assert handled == []
        assert event.answers == [("Нет доступа к этому каналу", True)]

    asyncio.run(run())


def test_foreign_post_task_content_plan_callback_is_denied_before_handler(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.core import settings_channel_access as access_module

        monkeypatch.setattr(
            access_module,
            "AsyncSessionLocal",
            lambda: _SessionContext(_FakeSession(task_channel_id=99)),
        )

        async def owns(session, *, tg_user_id: int, channel_id: int) -> bool:
            return int(tg_user_id) == 7001 and int(channel_id) == 12

        monkeypatch.setattr(access_module, "_user_owns_channel", owns)
        handled: list[bool] = []

        async def handler(event, data):
            handled.append(True)
            return "handled"

        event = _FakeEvent("cp_delete_post:44:2026-08-10")
        result = await SettingsChannelOwnerMiddleware()(handler, event, {})

        assert result is None
        assert handled == []
        assert event.answers == [("Нет доступа к этому каналу", True)]

    asyncio.run(run())


def test_owned_content_plan_callback_reaches_handler(monkeypatch) -> None:
    async def run() -> None:
        from app.core import settings_channel_access as access_module

        monkeypatch.setattr(
            access_module,
            "AsyncSessionLocal",
            lambda: _SessionContext(_FakeSession(task_channel_id=12)),
        )

        async def owns(session, *, tg_user_id: int, channel_id: int) -> bool:
            return int(tg_user_id) == 7001 and int(channel_id) == 12

        monkeypatch.setattr(access_module, "_user_owns_channel", owns)
        handled: list[str] = []

        async def handler(event, data):
            handled.append(str(event.data))
            return "handled"

        event = _FakeEvent("cp_open_post:44:2026-08-10")
        result = await SettingsChannelOwnerMiddleware()(handler, event, {})

        assert result == "handled"
        assert handled == ["cp_open_post:44:2026-08-10"]
        assert event.answers == []

    asyncio.run(run())
