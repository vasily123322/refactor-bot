from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.publication_scheduler import Scheduler


def test_scheduler_atomic_claim_filters_stale_second_worker(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "scheduler-claim.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as seed_session:
                item = await ContentRepo(seed_session).create(
                    channel_id=93,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Claim once"}]
                    ),
                )
                publication = await LegacyPublicationBridge(seed_session).queue(
                    content_item_id=item.id
                )
                publication_id = int(publication.id)
                task_id = int(publication.legacy_post_task_id or 0)
                assert task_id > 0

            async with Session() as first_session, Session() as second_session:
                # Both workers select while the row is still pending. End the read
                # transactions but retain stale ORM snapshots via expire_on_commit=False.
                first_task = await first_session.get(PostTask, task_id)
                second_task = await second_session.get(PostTask, task_id)
                assert first_task is not None and second_task is not None
                assert first_task.status == "pending"
                assert second_task.status == "pending"
                await first_session.commit()
                await second_session.commit()

                first_batch = [first_task]
                second_batch = [second_task]
                first_scheduler = Scheduler(first_session, SimpleNamespace())
                second_scheduler = Scheduler(second_session, SimpleNamespace())

                await first_scheduler._mark_processing(first_session, first_batch)
                assert [int(task.id) for task in first_batch] == [task_id]

                # The second ORM object still says pending, but the CAS UPDATE checks
                # the committed database status and removes the loser from its batch.
                assert second_task.status == "pending"
                await second_scheduler._mark_processing(second_session, second_batch)
                assert second_batch == []

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                assert task is not None
                assert task.status == "processing"

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
                assert attempts[0].status == "sending"
                assert attempts[0].finished_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())
