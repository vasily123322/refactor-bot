from __future__ import annotations

import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_errors import MISSING_SCHEDULER_TASK_ERROR


async def _seed(Session, *, channel_id: int) -> tuple[int, int, int]:
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Orphan transport"}]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id)
        )
        return (
            int(publication.id),
            int(publication.schedule_entry_id or 0),
            int(publication.legacy_post_task_id or 0),
        )


def test_reconcile_active_fails_queued_null_transport_without_inventing_attempt() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, schedule_id, task_id = await _seed(Session, channel_id=801)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                bridge = LegacyPublicationBridge(session)
                assert await bridge.reconcile_active() == 1
                assert await bridge.reconcile_active() == 0

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert publication is not None
                assert publication.status == "failed"
                assert publication.last_error == MISSING_SCHEDULER_TASK_ERROR
                assert publication.telegram_message_ids is None
                assert publication.result_link is None
                assert publication.attempt_count == 0
                assert schedule is not None and schedule.status == "failed"
                assert attempts == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sending_null_transport_finishes_same_active_attempt() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, schedule_id, task_id = await _seed(Session, channel_id=802)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "processing"
                await session.commit()
                publication = await LegacyPublicationBridge(session).reconcile_task(task)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1

                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                attempt_id = int(attempt.id)
                assert attempt.status == "sending"
                assert attempt.finished_at is None

                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                assert await LegacyPublicationBridge(session).reconcile_active() == 1

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert publication is not None
                assert publication.status == "failed"
                assert publication.last_error == MISSING_SCHEDULER_TASK_ERROR
                assert publication.attempt_count == 1
                assert schedule is not None and schedule.status == "failed"
                assert len(attempts) == 1
                assert int(attempts[0].id) == attempt_id
                assert attempts[0].attempt == 1
                assert attempts[0].status == "failed"
                assert attempts[0].error == MISSING_SCHEDULER_TASK_ERROR
                assert attempts[0].finished_at is not None
                assert attempts[0].meta["recovered_missing_transport"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reconcile_fails_stale_non_null_transport_id_when_task_row_is_missing() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, schedule_id, task_id = await _seed(Session, channel_id=803)

            async with Session() as session:
                # SQLite connections in the current compatibility setup do not enable
                # FK enforcement, so this models an existing stale non-null transport
                # ID. The bridge must handle it independently of DB FK behavior.
                await session.execute(delete(PostTask).where(PostTask.id == task_id))
                await session.commit()
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert int(publication.legacy_post_task_id or 0) == task_id

                publication = await LegacyPublicationBridge(session).reconcile(
                    publication_id
                )
                assert publication.status == "failed"
                assert publication.last_error == MISSING_SCHEDULER_TASK_ERROR

            async with Session() as session:
                schedule = await session.get(ScheduleEntry, schedule_id)
                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert schedule is not None and schedule.status == "failed"
                assert attempts == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_terminal_publication_is_not_downgraded_when_transport_disappears() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, schedule_id, task_id = await _seed(Session, channel_id=804)

            async with Session() as session:
                bridge = LegacyPublicationBridge(session)
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [80401],
                    "result_link": "https://t.me/example/80401",
                }
                await session.commit()
                publication = await bridge.reconcile(publication_id)
                assert publication.status == "published"
                assert publication.telegram_message_ids == [80401]
                assert publication.result_link == "https://t.me/example/80401"

                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                attempt_id = int(attempt.id)

                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                publication = await bridge.reconcile(publication_id)
                assert publication.status == "published"
                assert publication.last_error is None
                assert publication.telegram_message_ids == [80401]
                assert publication.result_link == "https://t.me/example/80401"

            async with Session() as session:
                schedule = await session.get(ScheduleEntry, schedule_id)
                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert schedule is not None and schedule.status == "completed"
                assert len(attempts) == 1
                assert int(attempts[0].id) == attempt_id
                assert attempts[0].status == "published"
                assert attempts[0].telegram_message_ids == [80401]
        finally:
            await engine.dispose()

    asyncio.run(run())
