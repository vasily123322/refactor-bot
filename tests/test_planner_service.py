from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.planner import PlannerConflictError, PlannerService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import as_utc


def test_planner_lists_and_reschedules_legacy_backed_publication_atomically() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=7,
                    title="Planner post",
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Hello"}]
                    ),
                )
                initial = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id,
                    scheduled_at=initial,
                    timezone_name="Europe/London",
                )

                planner = PlannerService(session)
                rows = await planner.list_entries(
                    channel_id=7,
                    start=initial - timedelta(days=1),
                    end=initial + timedelta(days=1),
                )
                assert len(rows) == 1
                assert rows[0].content_title == "Planner post"
                assert rows[0].publication_status == "queued"
                assert rows[0].scheduled_at == initial

                moved = datetime(2026, 8, 11, 15, 45, tzinfo=timezone.utc)
                entry = await planner.reschedule(
                    channel_id=7,
                    schedule_id=rows[0].schedule_id,
                    scheduled_at=moved,
                    timezone_name="UTC",
                )
                assert entry.scheduled_at == moved
                assert entry.timezone == "UTC"

                schedule = await session.get(ScheduleEntry, entry.schedule_id)
                task = await session.get(PostTask, publication.legacy_post_task_id)
                assert schedule is not None and task is not None
                assert as_utc(schedule.scheduled_at) == moved
                assert as_utc(task.scheduled_at) == moved
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_planner_cancel_updates_schedule_publication_and_scheduler_task() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=9,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Cancel me"}]
                    ),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id,
                    scheduled_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
                )
                assert publication.schedule_entry_id is not None

                entry = await PlannerService(session).cancel(
                    channel_id=9,
                    schedule_id=publication.schedule_entry_id,
                )
                assert entry.schedule_status == "cancelled"
                assert entry.publication_status == "cancelled"

                task = await session.get(PostTask, publication.legacy_post_task_id)
                stored_publication = await session.get(Publication, publication.id)
                assert task is not None and task.status == "cancelled"
                assert stored_publication is not None and stored_publication.status == "cancelled"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_planner_refuses_race_after_scheduler_started_processing() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=11,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Race"}]
                    ),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id,
                    scheduled_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
                )
                task = await session.get(PostTask, publication.legacy_post_task_id)
                assert task is not None
                task.status = "processing"
                await session.commit()

                planner = PlannerService(session)
                with pytest.raises(PlannerConflictError, match="scheduler task"):
                    await planner.reschedule(
                        channel_id=11,
                        schedule_id=publication.schedule_entry_id,
                        scheduled_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
                    )
                with pytest.raises(PlannerConflictError):
                    await planner.cancel(
                        channel_id=11,
                        schedule_id=publication.schedule_entry_id,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())
