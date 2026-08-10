from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.post_task_retention import PostTaskRetentionService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_published(
    Session,
    *,
    channel_id: int,
    now: datetime,
    repeat: bool = False,
    payload_updates: dict | None = None,
) -> tuple[int, int, int, int]:
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Successful retention {channel_id}",
                    }
                ]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(days=120),
            repeat_rule={"enabled": True, "seconds": 3600} if repeat else None,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        payload = {
            **dict(task.payload or {}),
            "result_ids": [channel_id * 100 + 1],
        }
        if payload_updates:
            payload.update(payload_updates)
        task.payload = payload
        task.status = "done"
        await session.commit()

        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == int(publication.id),
                    PublicationAttempt.attempt == int(publication.attempt_count),
                )
            )
        ).scalar_one()
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id or 0))
        assert schedule is not None
        attempt.finished_at = now - timedelta(days=120)
        await session.commit()
        return int(task.id), int(publication.id), int(attempt.id), int(schedule.id)


def test_success_retention_unlinks_old_exact_nonrepeat_publication(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'success-retention.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id, attempt_id, schedule_id = await _seed_published(
                Session,
                channel_id=931,
                now=now,
            )

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.eligible == 1
            assert tick.deleted == 1
            assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = await session.get(Publication, publication_id)
                attempt = await session.get(PublicationAttempt, attempt_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.legacy_post_task_id is None
                assert publication.telegram_message_ids == [93101]
                assert publication.meta["legacy_transport_retention"] == {
                    "retired": True,
                    "retired_at": now.isoformat(),
                    "terminal_status": "done",
                }
                assert attempt is not None
                assert attempt.status == "published"
                assert attempt.telegram_message_ids == [93101]
                assert schedule is not None and schedule.status == "completed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_success_retention_fails_closed_on_delivery_or_content_mismatch(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'success-retention-mismatch.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            delivery_task, delivery_publication, _, _ = await _seed_published(
                Session,
                channel_id=932,
                now=now,
            )
            content_task, content_publication, _, content_schedule = await _seed_published(
                Session,
                channel_id=933,
                now=now,
            )

            async with Session() as session:
                publication = await session.get(Publication, delivery_publication)
                schedule = await session.get(ScheduleEntry, content_schedule)
                assert publication is not None and schedule is not None
                publication.telegram_message_ids = [999999]
                schedule.channel_id = int(schedule.channel_id) + 10000
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)

            assert tick.selected == 2
            assert tick.deleted == 0
            assert tick.skipped_canonical_delivery == 1
            assert tick.skipped_content_linkage == 1
            async with Session() as session:
                assert await session.get(PostTask, delivery_task) is not None
                assert await session.get(PostTask, content_task) is not None
                assert await session.get(Publication, content_publication) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_success_retention_waits_for_canonical_autodelete_completion(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'success-retention-autodelete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            due_at = now - timedelta(days=119)
            task_id, publication_id, _, _ = await _seed_published(
                Session,
                channel_id=934,
                now=now,
                payload_updates={
                    "autodelete_seconds": 3600,
                    "autodelete_at": due_at.isoformat(),
                    "autodeleted": False,
                },
            )

            async with Session() as session:
                first = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)
                assert first.selected == 1
                assert first.deleted == 0
                assert first.skipped_pending_autodelete == 1
                assert await session.get(PostTask, task_id) is not None

                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                task.payload = {
                    **dict(task.payload or {}),
                    "autodeleted": True,
                    "autodeleted_at": now.isoformat(),
                }
                publication.meta = {
                    **dict(publication.meta or {}),
                    AUTODELETE_RUNTIME_META_KEY: {
                        "scheduled_at": due_at.isoformat(),
                        "effective_seconds": 3600,
                        "deleted": True,
                        "deleted_at": now.isoformat(),
                    },
                }
                await session.commit()

                second = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)
                assert second.deleted == 1

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id is None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_success_retention_still_excludes_repeat_lineage(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'success-retention-repeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, _, _, _ = await _seed_published(
                Session,
                channel_id=935,
                now=now,
                repeat=True,
            )

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)
                assert tick.selected == 1
                assert tick.deleted == 0
                assert tick.skipped_repeat == 1
                assert await session.get(PostTask, task_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
