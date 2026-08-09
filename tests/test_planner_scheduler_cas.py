from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
from app.workers.publication_scheduler import Scheduler


@pytest.mark.parametrize("action", ["reschedule", "cancel"])
def test_planner_mutation_loses_cleanly_if_scheduler_claims_stale_task(
    tmp_path,
    action: str,
) -> None:
    async def run() -> None:
        database_path = tmp_path / f"planner-{action}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            initial = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
            async with Session() as seed_session:
                item = await ContentRepo(seed_session).create(
                    channel_id=94,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Race-safe"}]
                    ),
                )
                publication = await LegacyPublicationBridge(seed_session).queue(
                    content_item_id=item.id,
                    scheduled_at=initial,
                )
                schedule_id = int(publication.schedule_entry_id or 0)
                publication_id = int(publication.id)
                task_id = int(publication.legacy_post_task_id or 0)
                assert schedule_id > 0 and task_id > 0

            async with Session() as planner_session:
                planner = PlannerService(planner_session)
                schedule, stale_publication, stale_task = await planner._locked_schedule(
                    channel_id=94,
                    schedule_id=schedule_id,
                )
                assert schedule.status == "pending"
                assert stale_publication is not None
                assert stale_publication.status == "queued"
                assert stale_task is not None
                assert stale_task.status == "pending"
                # End the read/lock transaction while retaining stale ORM snapshots.
                await planner_session.commit()

                async with Session() as scheduler_session:
                    task = await scheduler_session.get(PostTask, task_id)
                    assert task is not None
                    batch = [task]
                    scheduler = Scheduler(scheduler_session, SimpleNamespace())
                    await scheduler._mark_processing(scheduler_session, batch)
                    assert len(batch) == 1

                # Identity map still has queued/pending state, so the pre-check can
                # pass. The database CAS is the authoritative race detector.
                assert stale_publication.status == "queued"
                assert stale_task.status == "pending"

                with pytest.raises(
                    PlannerConflictError,
                    match="scheduler task is no longer pending",
                ):
                    if action == "reschedule":
                        await planner.reschedule(
                            channel_id=94,
                            schedule_id=schedule_id,
                            scheduled_at=initial + timedelta(days=1),
                        )
                    else:
                        await planner.cancel(
                            channel_id=94,
                            schedule_id=schedule_id,
                        )

            async with Session() as check_session:
                stored_schedule = await check_session.get(ScheduleEntry, schedule_id)
                stored_publication = await check_session.get(Publication, publication_id)
                stored_task = await check_session.get(PostTask, task_id)
                assert stored_schedule is not None
                assert stored_publication is not None
                assert stored_task is not None
                assert stored_schedule.status == "pending"
                assert as_utc(stored_schedule.scheduled_at) == initial
                assert stored_publication.status == "sending"
                assert stored_publication.attempt_count == 1
                assert stored_task.status == "processing"
        finally:
            await engine.dispose()

    asyncio.run(run())
