from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.bot.routers import main_router
from app.bot.routers.admin import (
    cmd_remove_allrepeat_disabled,
    router as admin_router,
)
from app.bot.routers.main import router as legacy_main_router
from app.core.config import settings


class _Message:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=int(user_id))
        self.answers: list[str] = []

    async def answer(self, text: str, *args, **kwargs):
        self.answers.append(str(text))
        return text


def test_admin_guard_router_precedes_legacy_main_router() -> None:
    """The fail-closed command handler must win before the old PostTask mutator."""

    assert admin_router in main_router.sub_routers
    assert legacy_main_router in main_router.sub_routers
    assert main_router.sub_routers.index(admin_router) < main_router.sub_routers.index(
        legacy_main_router
    )


def test_admin_bulk_repeat_guard_performs_no_legacy_mutation(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(settings, "admin_user_id", 987654321)
        message = _Message(987654321)
        await cmd_remove_allrepeat_disabled(message)  # type: ignore[arg-type]
        assert len(message.answers) == 1
        text = message.answers[0]
        assert "временно отключена" in text
        assert "мутация" in text

    asyncio.run(run())


def test_non_admin_bulk_repeat_guard_fails_closed(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(settings, "admin_user_id", 987654321)
        message = _Message(123)
        await cmd_remove_allrepeat_disabled(message)  # type: ignore[arg-type]
        assert message.answers == ["Недоступно"]

    asyncio.run(run())