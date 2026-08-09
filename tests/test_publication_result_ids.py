from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.telegram_results import normalize_telegram_message_ids


def test_normalize_telegram_message_ids_is_all_or_nothing() -> None:
    assert normalize_telegram_message_ids([101, "102", 103]) == [101, 102, 103]
    assert normalize_telegram_message_ids([]) == []
    assert normalize_telegram_message_ids("101") == []
    assert normalize_telegram_message_ids([101, "broken", 103]) == []
    assert normalize_telegram_message_ids([101, True]) == []
    assert normalize_telegram_message_ids([101, 0]) == []
    assert normalize_telegram_message_ids([101, -2]) == []
    assert normalize_telegram_message_ids([101, float("inf")]) == []


def test_publication_reconcile_ignores_malformed_legacy_result_ids() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=501,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Legacy"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                publication = await bridge.queue(content_item_id=int(item.id))
                task = await session.get(PostTask, int(publication.legacy_post_task_id))
                assert task is not None
                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [9001, "corrupt", 9002],
                    "result_link": "https://t.me/example/9002",
                }
                await session.commit()

                publication = await bridge.reconcile(int(publication.id))
                assert publication.status == "published"
                assert publication.telegram_message_ids is None
                assert publication.result_link == "https://t.me/example/9002"
                assert publication.attempt_count == 1

                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == int(publication.id)
                            )
                        )
                    ).scalars().all()
                )
                assert len(attempts) == 1
                assert attempts[0].status == "published"
                assert attempts[0].telegram_message_ids is None
                assert attempts[0].finished_at is not None

                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id),
                )
                assert schedule is not None
                assert schedule.status == "completed"

                # Reconciliation remains idempotent instead of failing every poll.
                publication = await bridge.reconcile(int(publication.id))
                assert publication.status == "published"
                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == int(publication.id)
                            )
                        )
                    ).scalars().all()
                )
                assert len(attempts) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
