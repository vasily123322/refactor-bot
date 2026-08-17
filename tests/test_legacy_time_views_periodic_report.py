from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.legacy_time_views_delete_action import LegacyTimeViewsDeleteAction
from app.domain.models import Channel, Client, PostTask
from app.services.legacy_time_views_delete_action_ledger import (
    LegacyTimeViewsDeleteActionLedger,
)


class _RecordingBot:
    def __init__(
        self,
        *,
        delete_error: Exception | None = None,
        report_error: Exception | None = None,
    ) -> None:
        self.delete_error = delete_error
        self.report_error = report_error
        self.delete_calls: list[tuple[int, int]] = []
        self.report_calls: list[tuple[int, str, bool]] = []

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
        self.report_calls.append(
            (int(chat_id), str(text), bool(disable_web_page_preview))
        )
        if self.report_error is not None:
            raise self.report_error


async def _fixture(*, report: bool = True):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    async with Session() as session:
        owner = Client(tg_user_id=99001, username=None, full_name=None)
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10099001,
            title="periodic report fixture",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        post = PostTask(
            channel_id=int(channel.id),
            status="done",
            payload={
                "autodelete_seconds": 60,
                "autodelete_effective_seconds": 60,
                "autodelete_views": 25,
                "autodelete_at": (now - timedelta(seconds=5)).isoformat(),
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
        return engine, Session, int(post.id), int(channel.tg_chat_id), int(owner.tg_user_id)


async def _as_periodic(coro):
    task = asyncio.current_task()
    assert task is not None
    previous = task.get_name()
    task.set_name("scheduler-autodelete")
    try:
        return await coro
    finally:
        task.set_name(previous)


def test_periodic_mixed_winner_reports_after_terminal_success_once() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, owner_id = await _fixture()
        bot = _RecordingBot()
        ledger = LegacyTimeViewsDeleteActionLedger(Session)
        try:
            first = await _as_periodic(
                ledger.delete_once(
                    bot=bot,
                    post_task_id=post_id,
                    chat_id=chat_id,
                    message_ids=(701,),
                )
            )
            assert first.outcome == "succeeded"
            assert bot.delete_calls == [(chat_id, 701)]
            assert bot.report_calls == [
                (
                    owner_id,
                    "🗑️ Пост удалён по таймеру\nhttps://t.me/example/701",
                    True,
                )
            ]

            async with Session() as session:
                action = (
                    await session.execute(
                        select(LegacyTimeViewsDeleteAction).where(
                            LegacyTimeViewsDeleteAction.post_task_id == post_id
                        )
                    )
                ).scalar_one()
                post = await session.get(PostTask, post_id)
                assert action.state == "succeeded"
                assert post is not None
                assert dict(post.payload or {}).get("autodeleted") is True

            replay = await _as_periodic(
                ledger.delete_once(
                    bot=bot,
                    post_task_id=post_id,
                    chat_id=chat_id,
                    message_ids=(701,),
                )
            )
            assert replay.outcome == "terminal"
            assert bot.delete_calls == [(chat_id, 701)]
            assert len(bot.report_calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_nonperiodic_winner_does_not_duplicate_caller_owned_report() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, _owner_id = await _fixture()
        bot = _RecordingBot()
        ledger = LegacyTimeViewsDeleteActionLedger(Session)
        try:
            result = await ledger.delete_once(
                bot=bot,
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=(701,),
            )
            assert result.outcome == "succeeded"
            assert bot.delete_calls == [(chat_id, 701)]
            assert bot.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_periodic_report_disabled_sends_nothing() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, _owner_id = await _fixture(report=False)
        bot = _RecordingBot()
        ledger = LegacyTimeViewsDeleteActionLedger(Session)
        try:
            result = await _as_periodic(
                ledger.delete_once(
                    bot=bot,
                    post_task_id=post_id,
                    chat_id=chat_id,
                    message_ids=(701,),
                )
            )
            assert result.outcome == "succeeded"
            assert bot.delete_calls == [(chat_id, 701)]
            assert bot.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_periodic_ambiguous_delete_never_sends_success_report_or_replays() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, _owner_id = await _fixture()
        first_bot = _RecordingBot(delete_error=RuntimeError("ambiguous timeout"))
        retry_bot = _RecordingBot()
        ledger = LegacyTimeViewsDeleteActionLedger(Session)
        try:
            first = await _as_periodic(
                ledger.delete_once(
                    bot=first_bot,
                    post_task_id=post_id,
                    chat_id=chat_id,
                    message_ids=(701,),
                )
            )
            assert first.outcome == "provider_unknown"
            assert first_bot.report_calls == []

            replay = await _as_periodic(
                ledger.delete_once(
                    bot=retry_bot,
                    post_task_id=post_id,
                    chat_id=chat_id,
                    message_ids=(701,),
                )
            )
            assert replay.outcome == "unknown"
            assert retry_bot.delete_calls == []
            assert retry_bot.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_periodic_report_failure_is_best_effort_and_delete_never_replays() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, _owner_id = await _fixture()
        first_bot = _RecordingBot(report_error=RuntimeError("report unavailable"))
        retry_bot = _RecordingBot()
        ledger = LegacyTimeViewsDeleteActionLedger(Session)
        try:
            first = await _as_periodic(
                ledger.delete_once(
                    bot=first_bot,
                    post_task_id=post_id,
                    chat_id=chat_id,
                    message_ids=(701,),
                )
            )
            assert first.outcome == "succeeded"
            assert first_bot.delete_calls == [(chat_id, 701)]
            assert len(first_bot.report_calls) == 1

            replay = await _as_periodic(
                ledger.delete_once(
                    bot=retry_bot,
                    post_task_id=post_id,
                    chat_id=chat_id,
                    message_ids=(701,),
                )
            )
            assert replay.outcome == "terminal"
            assert retry_bot.delete_calls == []
            assert retry_bot.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
