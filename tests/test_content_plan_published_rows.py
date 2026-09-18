from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.content_plan_published_rows import list_published_content_plan_rows
from app.services.posting import PostingService
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


class _Bot:
    pass


async def _seed_published(Session) -> tuple[int, int, int, int]:
    scheduled = datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc)
    async with Session() as session:
        owner = Client(
            tg_user_id=75001,
            username="owner",
            full_name="Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10075001,
            title="Canonical listing",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        channel_id = int(channel.id)

    created = await PostingService(_Bot(), Session).schedule(
        channel_id,
        {
            "type": "text",
            "text": "Canonical row survives transport retirement",
            "autodelete_seconds": 7200,
            "autodelete_views": 1500,
        },
        scheduled,
        dedupe_key="published-content-plan-canonical",
    )

    async with Session() as session:
        publication = await session.get(Publication, int(created.id))
        assert publication is not None and publication.schedule_entry_id is not None
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert schedule is not None

        task = PostTask(
            channel_id=channel_id,
            status="done",
            scheduled_at=scheduled,
            payload={
                "type": "text",
                "text": "Historical transport evidence",
                "result_ids": [7500101],
            },
        )
        session.add(task)
        await session.flush()

        publication.legacy_post_task_id = int(task.id)
        publication.status = "published"
        publication.telegram_message_ids = [7500101]
        schedule.status = "completed"
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": {
                "autodelete_seconds": 7200,
                "autodelete_views": 1500,
            },
            AUTODELETE_RUNTIME_META_KEY: {
                "deleted": True,
                "effective_seconds": 3600,
            },
        }
        await session.commit()
        return channel_id, int(publication.id), int(schedule.id), int(task.id)


def test_published_row_projects_link_presence_without_raw_post_task_identity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'published-content-plan-linked-row.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            channel_id, publication_id, _, _ = await _seed_published(Session)

            async with Session() as session:
                rows = await list_published_content_plan_rows(
                    session,
                    channel_id=channel_id,
                    start_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                    end_at=datetime(2026, 8, 11, tzinfo=timezone.utc) - timedelta(microseconds=1),
                )

            assert len(rows) == 1
            row = rows[0]
            assert row.publication_id == publication_id
            assert row.has_legacy_post_task_link is True
            assert "legacy_post_task_id" not in {field.name for field in fields(type(row))}
            assert not hasattr(row, "legacy_post_task_id")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_published_row_survives_post_task_retirement(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'published-content-plan-row.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            channel_id, publication_id, _, task_id = await _seed_published(Session)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                rows = await list_published_content_plan_rows(
                    session,
                    channel_id=channel_id,
                    start_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                    end_at=datetime(2026, 8, 11, tzinfo=timezone.utc) - timedelta(microseconds=1),
                )

            assert len(rows) == 1
            row = rows[0]
            assert row.publication_id == publication_id
            assert row.has_legacy_post_task_link is False
            assert not hasattr(row, "legacy_post_task_id")
            assert row.scheduled_at == datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc)
            assert row.title == "Canonical row survives transport retirement"
            assert row.autodeleted is True
            assert row.autodelete_seconds == 3600
            assert row.autodelete_views == 1500
            assert row.repeat_enabled is False
            assert row.repeat_seconds is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_inconsistent_schedule_linkage_is_not_listed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'published-content-plan-row-guards.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            channel_id, _, schedule_id, _ = await _seed_published(Session)

            async with Session() as session:
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert schedule is not None
                schedule.channel_id = channel_id + 99
                await session.commit()

                rows = await list_published_content_plan_rows(
                    session,
                    channel_id=channel_id,
                    start_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                    end_at=datetime(2026, 8, 11, tzinfo=timezone.utc) - timedelta(microseconds=1),
                )
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(run())
