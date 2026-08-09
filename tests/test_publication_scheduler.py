from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.publication_scheduler import Scheduler
from app.workers.reliable_scheduler import Scheduler as ReliableScheduler


def test_scheduler_projects_processing_and_terminal_publication_state(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as seed_session:
                item = await ContentRepo(seed_session).create(
                    channel_id=91,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Ship me"}]
                    ),
                )
                publication = await LegacyPublicationBridge(seed_session).queue(
                    content_item_id=item.id
                )
                publication_id = int(publication.id)
                task_id = int(publication.legacy_post_task_id or 0)
                assert task_id > 0

            scheduler = Scheduler(Session, SimpleNamespace())

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                await scheduler._mark_processing(scheduler_session, [task])

            async with Session() as check_session:
                sending = await check_session.get(Publication, publication_id)
                assert sending is not None
                assert sending.status == "sending"
                assert sending.attempt_count == 0

            async def fake_process_items(self, session, items):
                assert len(items) == 1
                post = items[0]
                post.status = "done"
                post.payload = {
                    **dict(post.payload or {}),
                    "result_ids": [701, 702],
                    "result_link": "https://t.me/example/702",
                }
                await session.commit()

            monkeypatch.setattr(ReliableScheduler, "_process_items", fake_process_items)

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                await scheduler._process_items(scheduler_session, [task])

            async with Session() as check_session:
                published = await check_session.get(Publication, publication_id)
                assert published is not None
                assert published.status == "published"
                assert published.telegram_message_ids == [701, 702]
                assert published.result_link == "https://t.me/example/702"
                assert published.attempt_count == 1

                schedule = await check_session.get(
                    ScheduleEntry, int(published.schedule_entry_id or 0)
                )
                assert schedule is not None
                assert schedule.status == "completed"

                attempts = list(
                    (
                        await check_session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert len(attempts) == 1
                assert attempts[0].status == "published"
                assert attempts[0].telegram_message_ids == [701, 702]

            # Projection is idempotent: recovery/reconciler can safely repeat it.
            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                await scheduler._project_publication(scheduler_session, task)
            async with Session() as check_session:
                attempts = list(
                    (
                        await check_session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert len(attempts) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
