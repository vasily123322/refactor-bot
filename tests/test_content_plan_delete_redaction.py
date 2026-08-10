from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _SessionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, model, object_id: int):
        return SimpleNamespace(id=int(object_id), channel_id=12)

    async def delete(self, obj) -> None:
        raise RuntimeError("postgres://user:secret-password@db.internal/private")

    async def commit(self) -> None:
        raise AssertionError("commit must not run after delete failure")


class _Callback:
    def __init__(self) -> None:
        self.data = "cp_delete_post:44:2026-08-10"
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", *, show_alert: bool = False):
        self.answers.append((str(text), bool(show_alert)))
        return None


class _State:
    async def get_data(self) -> dict:
        return {"cp_channel_id": 12}


def test_delete_failure_does_not_expose_raw_exception(monkeypatch) -> None:
    async def run() -> None:
        from app.bot.routers import content_plan

        monkeypatch.setattr(content_plan, "AsyncSessionLocal", _SessionContext)
        callback = _Callback()

        await content_plan.cb_cp_delete_post(callback, _State())  # type: ignore[arg-type]

        assert callback.answers == [("Не удалось удалить", True)]
        rendered = " ".join(text for text, _ in callback.answers)
        assert "secret-password" not in rendered
        assert "db.internal" not in rendered
        assert "postgres://" not in rendered

    asyncio.run(run())
