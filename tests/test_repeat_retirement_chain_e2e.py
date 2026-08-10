from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.post_task_retention import PostTaskRetentionService
from app.services.scheduling import cleanup_runtime_fields, inherit_flags_for_repeat
from app.workers.publication_scheduler import Scheduler as PublicationScheduler


def test_retired_repeat_root_hands_off_to_scheduler_and_canonical_grandchild(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-retirement-chain.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            root_when = now - timedelta(hours=2)

            async with Session() as session:
                root = PostTask(
                    channel_id=981,
                    status="done",
                    scheduled_at=root_when,
                    payload={
                        "type": "text",
                        "text": "Repeat chain root",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "result_ids": [98101],
                    },
                )
                session.add(root)
                await session.commit()
                await session.refresh(root)
                root_task_id = int(root.id)

                root_publication = await mirror_legacy_post_task(session, root)
                assert root_publication is not None
                root_publication_id = int(root_publication.id)
                root_content_item_id = int(root_publication.content_item_id)
                root_content_revision = int(root_publication.content_revision)
                root_attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == root_publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                root_attempt.finished_at = now - timedelta(days=120)
                await session.commit()

                child_payload = cleanup_runtime_fields(dict(root.payload or {}))
                child_payload = inherit_flags_for_repeat(child_payload, root_task_id)
                child = PostTask(
                    channel_id=981,
                    status="pending",
                    scheduled_at=root_when + timedelta(hours=1),
                    payload=child_payload,
                )
                session.add(child)
                await session.commit()
                await session.refresh(child)
                child_task_id = int(child.id)
                child_publication = await mirror_legacy_post_task(session, child)
                assert child_publication is not None
                child_publication_id = int(child_publication.id)

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_repeat_occurrences=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.deleted == 1
            assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, root_task_id) is None
                root_publication = await session.get(Publication, root_publication_id)
                child = await session.get(PostTask, child_task_id)
                child_publication = await session.get(
                    Publication,
                    child_publication_id,
                )
                assert root_publication is not None
                assert child is not None and child_publication is not None
                assert root_publication.legacy_post_task_id is None
                assert child_publication.legacy_post_task_id == child_task_id
                retention_meta = root_publication.meta["legacy_transport_retention"]
                assert retention_meta["repeat_group_id"] == root_task_id
                assert retention_meta["repeat_successor_task_id"] == child_task_id

                scheduler = PublicationScheduler(session, object())
                await scheduler._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    child,
                    dict(child.payload or {}),
                )

                grandchild = (
                    await session.execute(
                        select(PostTask)
                        .where(
                            PostTask.id != child_task_id,
                            PostTask.channel_id == 981,
                            PostTask.status == "pending",
                            PostTask.payload["repeat_group_id"].as_integer()
                            == root_task_id,
                            PostTask.scheduled_at > child.scheduled_at,
                        )
                        .order_by(PostTask.scheduled_at.asc(), PostTask.id.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                assert grandchild is not None
                grandchild_publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(grandchild.id)
                        )
                    )
                ).scalar_one_or_none()
                assert grandchild_publication is not None
                grandchild_schedule = await session.get(
                    ScheduleEntry,
                    int(grandchild_publication.schedule_entry_id or 0),
                )
                assert grandchild_schedule is not None

                assert grandchild_publication.content_item_id == root_content_item_id
                assert grandchild_publication.content_revision == root_content_revision
                assert grandchild_schedule.content_item_id == root_content_item_id
                assert grandchild_schedule.content_revision == root_content_revision
                assert grandchild_publication.meta["repeat_group_id"] == root_task_id
                assert grandchild_schedule.meta["repeat_group_id"] == root_task_id
                assert grandchild_schedule.repeat_rule == {
                    "enabled": True,
                    "seconds": 3600,
                }
                assert child_publication.content_item_id == root_content_item_id
                assert child_publication.content_revision == root_content_revision
        finally:
            await engine.dispose()

    asyncio.run(run())
