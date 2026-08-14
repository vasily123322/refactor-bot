from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.routers import posting_publish
from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.manual_publish import ManualPublishService


class _RecordingBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[int] = []

    async def send_message(self, *, chat_id: int, **kwargs):
        self.calls.append(int(chat_id))
        if self.fail:
            raise RuntimeError("ambiguous provider failure")
        return SimpleNamespace(message_id=10_000 + len(self.calls))


async def _new_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _channel(Session, suffix: int, *, premium: bool = True) -> Channel:
    async with Session() as session:
        owner = Client(
            tg_user_id=9_950_000 + suffix,
            username=f"manual{suffix}",
            full_name="Manual Publish Fixture",
            is_premium=premium,
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1_009_950_000_000 - suffix,
            title=f"Manual {suffix}",
            owner_id=int(owner.id),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


def _same_instant(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


def test_manual_publish_sends_once_and_creates_one_linked_repeat_root() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 1)
            bot = _RecordingBot()
            now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
            result = await ManualPublishService(bot, Session).publish(
                channel_id=int(channel.id),
                payload={"type": "text", "text": "Manual repeat root"},
                notify_on=True,
                repeat_on=True,
                repeat_seconds=3600,
                repeat_allowed=True,
                now=now,
            )

            assert result.sent is True
            assert bot.calls == [int(channel.tg_chat_id)]
            assert result.repeat_post_task_id is not None

            async with Session() as session:
                tasks = list((await session.execute(select(PostTask))).scalars().all())
                publications = list(
                    (await session.execute(select(Publication))).scalars().all()
                )
                schedules = list(
                    (await session.execute(select(ScheduleEntry))).scalars().all()
                )
                assert len(tasks) == len(publications) == len(schedules) == 1
                task = tasks[0]
                publication = publications[0]
                schedule = schedules[0]
                assert int(task.id) == int(result.repeat_post_task_id)
                assert task.channel_id == int(channel.id)
                assert task.status == "pending"
                assert _same_instant(
                    task.scheduled_at,
                    now + timedelta(seconds=3600),
                )
                assert publication.legacy_post_task_id == int(task.id)
                assert publication.status == "queued"
                assert publication.schedule_entry_id == int(schedule.id)
                assert schedule.status == "pending"
                assert dict(publication.meta or {}).get("repeat_group_id") == int(task.id)
                assert dict(schedule.meta or {}).get("repeat_group_id") == int(task.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_failure_creates_no_future_repeat_work() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 2)
            bot = _RecordingBot(fail=True)
            result = await ManualPublishService(bot, Session).publish(
                channel_id=int(channel.id),
                payload={"type": "text", "text": "Do not schedule after failure"},
                repeat_on=True,
                repeat_seconds=3600,
                repeat_allowed=True,
            )

            assert result.sent is False
            assert bot.calls == [int(channel.tg_chat_id)]
            assert result.repeat_post_task_id is None
            async with Session() as session:
                assert list((await session.execute(select(PostTask))).scalars()) == []
                assert list((await session.execute(select(Publication))).scalars()) == []
                assert list((await session.execute(select(ScheduleEntry))).scalars()) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_forward_targets_use_telegram_ids_without_duplicating_repeat_root() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            primary = await _channel(Session, 3)
            secondary = await _channel(Session, 4)
            bot = _RecordingBot()
            result = await ManualPublishService(bot, Session).publish(
                channel_id=int(primary.id),
                payload={"type": "text", "text": "Forward once"},
                forward_to=[int(secondary.id), int(secondary.id), int(primary.id)],
                repeat_on=True,
                repeat_seconds=1800,
                repeat_allowed=True,
            )

            assert result.sent is True
            assert bot.calls == [int(primary.tg_chat_id), int(secondary.tg_chat_id)]
            assert result.forwarded_targets == (int(secondary.id),)
            async with Session() as session:
                tasks = list((await session.execute(select(PostTask))).scalars().all())
                publications = list(
                    (await session.execute(select(Publication))).scalars().all()
                )
                assert len(tasks) == 1
                assert len(publications) == 1
                assert tasks[0].channel_id == int(primary.id)
                assert publications[0].legacy_post_task_id == int(tasks[0].id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_send_handler_has_no_self_recursion() -> None:
    source = textwrap.dedent(inspect.getsource(posting_publish.cb_post_send))
    tree = ast.parse(source)
    recursive_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cb_post_send"
    ]
    assert recursive_calls == []
    assert "ManualPublishService" in source
