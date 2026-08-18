from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.legacy_terminal_content_mirror import (
    mirror_unlinked_terminal_legacy_tasks,
)


async def _new_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _channel(Session, suffix: int) -> Channel:
    async with Session() as session:
        owner = Client(
            tg_user_id=9_960_000 + suffix,
            username=f"terminalmirror{suffix}",
            full_name="Terminal Mirror Fixture",
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1_009_960_000_000 - suffix,
            title=f"Terminal mirror {suffix}",
            owner_id=int(owner.id),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


async def _task(Session, channel_id: int, suffix: int, *, status: str, payload: dict):
    async with Session() as session:
        task = PostTask(
            channel_id=int(channel_id),
            status=status,
            payload=payload,
            dedupe_key=f"terminal-mirror-{suffix}",
            scheduled_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return int(task.id)


def test_pending_supported_row_is_not_relinked_while_legacy_can_claim_it() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 1)
            task_id = await _task(
                Session,
                int(channel.id),
                1,
                status="pending",
                payload={"type": "text", "text": "historical active supported"},
            )
            async with Session() as session:
                mirrored, skipped = await mirror_unlinked_terminal_legacy_tasks(session)
                assert (mirrored, skipped) == (0, 0)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == task_id
                        )
                    )
                ).scalar_one_or_none()
                assert task is not None and task.status == "pending"
                assert publication is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_processing_supported_row_is_not_relinked_mid_execution() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 2)
            task_id = await _task(
                Session,
                int(channel.id),
                2,
                status="processing",
                payload={"type": "text", "text": "legacy execution in progress"},
            )
            async with Session() as session:
                mirrored, skipped = await mirror_unlinked_terminal_legacy_tasks(session)
                assert (mirrored, skipped) == (0, 0)

            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                assert (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == task_id
                        )
                    )
                ).scalar_one_or_none() is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_terminal_supported_row_is_mirrored_as_history_only() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 3)
            task_id = await _task(
                Session,
                int(channel.id),
                3,
                status="done",
                payload={
                    "type": "text",
                    "text": "completed legacy history",
                    "result_ids": [777],
                    "result_link": "https://t.me/example/777",
                },
            )
            async with Session() as session:
                mirrored, skipped = await mirror_unlinked_terminal_legacy_tasks(session)
                assert (mirrored, skipped) == (1, 0)

            async with Session() as session:
                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == task_id
                        )
                    )
                ).scalar_one()
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id)
                )
                assert publication.status == "published"
                assert publication.attempt_count == 1
                assert publication.telegram_message_ids == [777]
                assert schedule is not None and schedule.status == "completed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_active_mixed_time_views_remains_legacy_owned_and_unmirrored() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 4)
            task_id = await _task(
                Session,
                int(channel.id),
                4,
                status="pending",
                payload={
                    "type": "text",
                    "text": "mixed legacy owner",
                    "autodelete_seconds": 600,
                    "delete_after_views": 100,
                },
            )
            async with Session() as session:
                await mirror_unlinked_terminal_legacy_tasks(session)
            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                assert (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == task_id
                        )
                    )
                ).scalar_one_or_none() is None
        finally:
            await engine.dispose()

    asyncio.run(run())
