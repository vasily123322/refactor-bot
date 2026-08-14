from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.legacy_time_views_delete_action import LegacyTimeViewsDeleteAction
from app.domain.models import Channel, Client, PostTask
from app.services.legacy_time_views_delete_action_ledger import (
    LegacyTimeViewsDeleteActionLedger,
)
from app.workers.scheduler import Scheduler


class _RecordingBot:
    def __init__(self, *, delete_error: Exception | None = None):
        self.delete_error = delete_error
        self.delete_calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.delete_calls.append((int(chat_id), int(message_id)))
        if self.delete_error is not None:
            raise self.delete_error

    async def send_message(self, *args, **kwargs) -> None:
        return None


async def _new_fixture(*, message_ids: tuple[int, ...] = (701,)):
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
            title="legacy mixed fixture",
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
                "autodeleted": False,
                "result_ids": list(message_ids),
            },
            dedupe_key=None,
            scheduled_at=now - timedelta(minutes=5),
        )
        session.add(post)
        await session.commit()
        return engine, Session, int(post.id), int(channel.tg_chat_id), list(message_ids)


def _scheduler(Session, bot: _RecordingBot) -> Scheduler:
    scheduler = Scheduler(Session, SimpleNamespace(bot=bot), interval_seconds=1)
    scheduler._legacy_time_views_delete_ledger = LegacyTimeViewsDeleteActionLedger(
        Session
    )
    return scheduler


def test_local_winner_prevents_periodic_provider_delete() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _new_fixture()
        local_bot = _RecordingBot()
        periodic_bot = _RecordingBot()
        scheduler = _scheduler(Session, periodic_bot)
        try:
            await scheduler._del_later(
                local_bot,
                chat_id,
                message_ids,
                0,
                post_id,
                False,
                None,
            )
            assert local_bot.delete_calls == [(chat_id, message_ids[0])]

            async with Session() as session:
                await scheduler._process_due_deletions(session)
            assert periodic_bot.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_periodic_winner_prevents_local_provider_delete() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _new_fixture()
        periodic_bot = _RecordingBot()
        local_bot = _RecordingBot()
        scheduler = _scheduler(Session, periodic_bot)
        try:
            async with Session() as session:
                await scheduler._process_due_deletions(session)
            assert periodic_bot.delete_calls == [(chat_id, message_ids[0])]

            await scheduler._del_later(
                local_bot,
                chat_id,
                message_ids,
                0,
                post_id,
                False,
                None,
            )
            assert local_bot.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ambiguous_provider_failure_is_never_replayed() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _new_fixture()
        first_bot = _RecordingBot(delete_error=RuntimeError("ambiguous timeout"))
        retry_bot = _RecordingBot()
        scheduler = _scheduler(Session, retry_bot)
        try:
            await scheduler._del_later(
                first_bot,
                chat_id,
                message_ids,
                0,
                post_id,
                False,
                None,
            )
            assert first_bot.delete_calls == [(chat_id, message_ids[0])]

            async with Session() as session:
                action = (
                    await session.execute(
                        select(LegacyTimeViewsDeleteAction).where(
                            LegacyTimeViewsDeleteAction.post_task_id == post_id
                        )
                    )
                ).scalar_one()
                assert action.state == "unknown"

            async with Session() as session:
                await scheduler._process_due_deletions(session)
            assert retry_bot.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_committed_reserved_survives_restart_without_reauthorization() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _new_fixture()
        try:
            first_process = LegacyTimeViewsDeleteActionLedger(Session)
            reserved = await first_process.reserve(
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert reserved.outcome == "reserved"
            assert reserved.reservation is not None

            restarted_process = LegacyTimeViewsDeleteActionLedger(Session)
            bot = _RecordingBot()
            result = await restarted_process.delete_once(
                bot=bot,
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert result.outcome == "already_reserved"
            assert bot.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_reservation_token_cannot_finalize() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _new_fixture()
        ledger = LegacyTimeViewsDeleteActionLedger(Session)
        try:
            reserved = await ledger.reserve(
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert reserved.reservation is not None
            stale = replace(reserved.reservation, token="stale-token")
            assert await ledger.mark_succeeded(stale) is False

            async with Session() as session:
                action = (
                    await session.execute(
                        select(LegacyTimeViewsDeleteAction).where(
                            LegacyTimeViewsDeleteAction.post_task_id == post_id
                        )
                    )
                ).scalar_one()
                assert action.state == "reserved"
                assert action.finalized_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_wrong_exact_target_cannot_finalize() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _new_fixture()
        ledger = LegacyTimeViewsDeleteActionLedger(Session)
        try:
            reserved = await ledger.reserve(
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert reserved.reservation is not None
            reservation = reserved.reservation

            wrong_chat_id = chat_id - 1
            wrong_chat_ids = tuple(message_ids)
            wrong_chat = replace(
                reservation,
                chat_id=wrong_chat_id,
                target_fingerprint=ledger._target_fingerprint(
                    post_task_id=post_id,
                    chat_id=wrong_chat_id,
                    message_ids=wrong_chat_ids,
                ),
            )
            assert await ledger.mark_succeeded(wrong_chat) is False

            wrong_message_ids = (message_ids[0] + 1,)
            wrong_message = replace(
                reservation,
                message_ids=wrong_message_ids,
                target_fingerprint=ledger._target_fingerprint(
                    post_task_id=post_id,
                    chat_id=chat_id,
                    message_ids=wrong_message_ids,
                ),
            )
            assert await ledger.mark_succeeded(wrong_message) is False

            wrong_fingerprint = replace(
                reservation,
                target_fingerprint="0" * 64,
            )
            assert await ledger.mark_succeeded(wrong_fingerprint) is False

            async with Session() as session:
                action = (
                    await session.execute(
                        select(LegacyTimeViewsDeleteAction).where(
                            LegacyTimeViewsDeleteAction.post_task_id == post_id
                        )
                    )
                ).scalar_one()
                assert action.state == "reserved"
                assert action.finalized_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_successful_winner_deletes_once_and_exactly_finalizes_terminal() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _new_fixture()
        ledger = LegacyTimeViewsDeleteActionLedger(Session)
        bot = _RecordingBot()
        try:
            result = await ledger.delete_once(
                bot=bot,
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert result.outcome == "succeeded"
            assert result.reservation is not None
            assert bot.delete_calls == [(chat_id, message_ids[0])]

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
                assert action.finalized_at is not None
                assert action.chat_id == chat_id
                assert action.message_ids == message_ids
                assert action.reservation_token == result.reservation.token
                assert (
                    action.target_fingerprint
                    == result.reservation.target_fingerprint
                )
                assert post is not None
                assert dict(post.payload or {}).get("autodeleted") is True

            replay = await ledger.delete_once(
                bot=bot,
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert replay.outcome == "terminal"
            assert bot.delete_calls == [(chat_id, message_ids[0])]

            # The authority remains a legacy mixed fallback: time-only tasks are
            # explicitly outside this ledger and retain their historical path.
            async with Session() as session:
                original = await session.get(PostTask, post_id)
                assert original is not None
                time_only_payload = dict(original.payload or {})
                time_only_payload["autodelete_views"] = 0
                time_only_payload["autodeleted"] = False
                time_only = PostTask(
                    channel_id=int(original.channel_id),
                    status="done",
                    payload=time_only_payload,
                    dedupe_key=None,
                    scheduled_at=original.scheduled_at,
                )
                session.add(time_only)
                await session.commit()
                time_only_id = int(time_only.id)

            fallback = await ledger.reserve(
                post_task_id=time_only_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert fallback.outcome == "not_applicable"
        finally:
            await engine.dispose()

    asyncio.run(run())
