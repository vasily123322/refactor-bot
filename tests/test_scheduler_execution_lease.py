from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_task_lease import SchedulerTaskLeaseService
from app.workers.publication_scheduler import Scheduler
from app.workers.reliable_scheduler import Scheduler as ReliableScheduler


async def _seed(Session) -> tuple[int, int]:
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=96,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Lease me"}]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(content_item_id=item.id)
        return int(publication.id), int(publication.legacy_post_task_id or 0)


def test_scheduler_releases_lease_after_terminal_processing(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'terminal-lease.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(Session)
            scheduler = Scheduler(Session, SimpleNamespace())

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                batch = [task]
                await scheduler._mark_processing(scheduler_session, batch)
                assert len(batch) == 1

            async with Session() as check_session:
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                publication = await check_session.get(Publication, publication_id)
                assert lease is not None
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1

            async def fake_process_items(self, session, items):
                assert len(items) == 1
                post = items[0]
                post.status = "done"
                post.payload = {
                    **dict(post.payload or {}),
                    "result_ids": [9601],
                }
                await session.commit()

            monkeypatch.setattr(ReliableScheduler, "_process_items", fake_process_items)

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                await scheduler._process_items(scheduler_session, [task])

            async with Session() as check_session:
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                publication = await check_session.get(Publication, publication_id)
                task = await check_session.get(PostTask, task_id)
                assert lease is None
                assert task is not None and task.status == "done"
                assert publication is not None and publication.status == "published"
                assert publication.telegram_message_ids == [9601]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_lease_after_cancelled_processing(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'cancelled-lease.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(Session)
            scheduler = Scheduler(Session, SimpleNamespace())

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                batch = [task]
                await scheduler._mark_processing(scheduler_session, batch)
                assert len(batch) == 1

            async def cancelled_process_items(self, session, items):
                raise asyncio.CancelledError

            monkeypatch.setattr(
                ReliableScheduler,
                "_process_items",
                cancelled_process_items,
            )

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                with pytest.raises(asyncio.CancelledError):
                    await scheduler._process_items(scheduler_session, [task])

            async with Session() as check_session:
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert lease is not None
                assert task is not None and task.status == "processing"
                assert publication is not None and publication.status == "sending"
                assert publication.attempt_count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
