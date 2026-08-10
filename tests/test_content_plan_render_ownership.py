from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace


class _DeniedResult:
    def scalar_one_or_none(self):
        return None


class _DeniedSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, statement):
        return _DeniedResult()


class _Callback:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=7001)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", *, show_alert: bool = False):
        self.answers.append((str(text), bool(show_alert)))
        return None


class _State:
    pass


def test_render_content_plan_denies_foreign_channel_from_stale_state(monkeypatch) -> None:
    async def run() -> None:
        from app.bot.routers import content_plan

        monkeypatch.setattr(content_plan, "AsyncSessionLocal", _DeniedSession)
        callback = _Callback()

        await content_plan._render_content_plan(  # noqa: SLF001 - security boundary regression
            callback,  # type: ignore[arg-type]
            _State(),  # type: ignore[arg-type]
            99,
            datetime(2026, 8, 10, tzinfo=timezone.utc),
        )

        assert callback.answers == [("Нет доступа к этому каналу", True)]

    asyncio.run(run())
