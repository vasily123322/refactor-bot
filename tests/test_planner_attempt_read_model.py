from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.repositories.content import ContentRepo
from app.services.planner import PlannerService
from app.services.publication_bridge import LegacyPublicationBridge


def test_planner_reads_current_publication_attempt_without_legacy_payload() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                scheduled_at = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
                item = await ContentRepo(session).create(
                    channel_id=92,
                    title="Observable delivery",
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Hello"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                publication = await bridge.queue(
                    content_item_id=item.id,
                    scheduled_at=scheduled_at,
                )
                task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
                assert task is not None

                task.status = "processing"
                await session.commit()
                await bridge.reconcile_task(task)

                planner = PlannerService(session)
                rows = await planner.list_entries(
                    channel_id=92,
                    start=scheduled_at - timedelta(days=1),
                    end=scheduled_at + timedelta(days=1),
                )
                assert len(rows) == 1
                sending = rows[0]
                assert sending.publication_status == "sending"
                assert sending.attempt_number == 1
                assert sending.attempt_status == "sending"
                assert sending.attempt_started_at is not None
                assert sending.attempt_finished_at is None

                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [901],
                }
                await session.commit()
                await bridge.reconcile_task(task)

                finished = await planner.get_entry(
                    channel_id=92,
                    schedule_id=sending.schedule_id,
                )
                assert finished.publication_status == "published"
                assert finished.attempt_number == 1
                assert finished.attempt_status == "published"
                assert finished.attempt_started_at == sending.attempt_started_at
                assert finished.attempt_finished_at is not None
                assert finished.telegram_message_ids == [901]
        finally:
            await engine.dispose()

    asyncio.run(run())
