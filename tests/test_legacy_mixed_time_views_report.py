from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.services.legacy_mixed_time_views_autodelete import (
    LegacyMixedTimeViewsAutodeleteObserver,
)


class _Views:
    async def get_message_views(self, target: int, message_id: int) -> int:
        return 100


class _Bot:
    def __init__(
        self,
        *,
        delete_error: Exception | None = None,
        report_error: Exception | None = None,
    ) -> None:
        self.delete_error = delete_error
        self.report_error = report_error
        self.delete_calls: list[tuple[int, int]] = []
        self.report_calls: list[tuple[int, str]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.delete_calls.append((int(chat_id), int(message_id)))
        if self.delete_error is not None:
            raise self.delete_error

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ) -> None:
        assert disable_web_page_preview is True
        self.report_calls.append((int(chat_id), str(text)))
        if self.report_error is not None:
            raise self.report_error


async def _fixture(*, report: bool):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    async with Session() as session:
        owner = Client(tg_user_id=9001, username=None, full_name=None)
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1009001001,
            title="mixed report fixture",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        post = PostTask(
            channel_id=int(channel.id),
            status="done",
            payload={
                "autodelete_seconds": 3600,
                "autodelete_effective_seconds": 3600,
                "autodelete_views": 25,
                "autodelete_at": (now + timedelta(hours=1)).isoformat(),
                "autodelete_report": report,
                "autodeleted": False,
                "result_ids": [701],
                "result_link": "https://t.me/example/701",
            },
            dedupe_key=None,
            scheduled_at=now - timedelta(minutes=5),
        )
        session.add(post)
        await session.commit()
        return engine, Session, int(channel.tg_chat_id), int(owner.tg_user_id)


def _observer(Session, bot: _Bot) -> LegacyMixedTimeViewsAutodeleteObserver:
    return LegacyMixedTimeViewsAutodeleteObserver(
        view_source=_Views(),
        delete_provider=bot,
        session_factory=Session,
    )


def test_views_winner_sends_requested_report_after_terminal_delete() -> None:
    async def run() -> None:
        engine, Session, chat_id, owner_id = await _fixture(report=True)
        bot = _Bot()
        try:
            tick = await _observer(Session, bot).run_once()
            assert tick.delete_winners == 1
            assert bot.delete_calls == [(chat_id, 701)]
            assert bot.report_calls == [
                (
                    owner_id,
                    "🗑️ Пост удалён по просмотрам\nhttps://t.me/example/701",
                )
            ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_views_winner_does_not_report_when_option_disabled() -> None:
    async def run() -> None:
        engine, Session, chat_id, _owner_id = await _fixture(report=False)
        bot = _Bot()
        try:
            tick = await _observer(Session, bot).run_once()
            assert tick.delete_winners == 1
            assert bot.delete_calls == [(chat_id, 701)]
            assert bot.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_failure_is_best_effort_after_delete_and_never_replays_delete() -> None:
    async def run() -> None:
        engine, Session, chat_id, owner_id = await _fixture(report=True)
        first = _Bot(report_error=RuntimeError("report unavailable"))
        second = _Bot()
        try:
            first_tick = await _observer(Session, first).run_once()
            assert first_tick.delete_winners == 1
            assert first.delete_calls == [(chat_id, 701)]
            assert first.report_calls == [
                (
                    owner_id,
                    "🗑️ Пост удалён по просмотрам\nhttps://t.me/example/701",
                )
            ]

            restarted = await _observer(Session, second).run_once()
            assert restarted.selected == 0
            assert second.delete_calls == []
            assert second.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ambiguous_delete_never_sends_success_report() -> None:
    async def run() -> None:
        engine, Session, chat_id, _owner_id = await _fixture(report=True)
        bot = _Bot(delete_error=RuntimeError("ambiguous timeout"))
        try:
            tick = await _observer(Session, bot).run_once()
            assert tick.delete_winners == 0
            assert tick.already_handled == 1
            assert bot.delete_calls == [(chat_id, 701)]
            assert bot.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
