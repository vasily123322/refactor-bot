from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import as_utc


def test_publication_bridge_queues_content_on_existing_scheduler() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                content_repo = ContentRepo(session)
                item = await content_repo.create(
                    channel_id=99,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Publish me"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                when = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
                publication = await bridge.queue(
                    content_item_id=item.id,
                    scheduled_at=when,
                    timezone_name="Europe/London",
                    repeat_rule={"enabled": True, "seconds": 3600},
                    runtime_options={"pin": True},
                )

                assert publication.status == "queued"
                assert publication.content_revision == 1
                assert publication.legacy_post_task_id is not None

                task = await session.get(PostTask, publication.legacy_post_task_id)
                assert task is not None
                assert as_utc(task.scheduled_at) == when
                assert task.payload["text"] == "Publish me"
                assert task.payload["repeat_on"] is True
                assert task.payload["repeat_seconds"] == 3600
                assert task.payload["pin"] is True
                assert task.payload["_publication_id"] == publication.id
                assert task.dedupe_key == f"publication:{publication.id}"

                schedule = await session.get(ScheduleEntry, publication.schedule_entry_id)
                assert schedule is not None
                assert as_utc(schedule.scheduled_at) == when
                assert schedule.content_item_id == item.id
                assert schedule.content_revision == 1
                assert schedule.repeat_rule["seconds"] == 3600
                assert schedule.meta["legacy_post_task_id"] == task.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_bridge_reconciles_success_and_records_attempt() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=1,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Hello"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                publication = await bridge.queue(content_item_id=item.id)
                task = await session.get(PostTask, publication.legacy_post_task_id)
                assert task is not None
                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [101, 102],
                    "result_link": "https://t.me/example/102",
                }
                await session.commit()

                publication = await bridge.reconcile(publication.id)
                assert publication.status == "published"
                assert publication.telegram_message_ids == [101, 102]
                assert publication.result_link == "https://t.me/example/102"
                assert publication.attempt_count == 1

                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication.id
                        )
                    )
                ).scalars().all()
                assert len(attempts) == 1
                assert attempts[0].status == "published"
                assert attempts[0].telegram_message_ids == [101, 102]

                schedule = await session.get(ScheduleEntry, publication.schedule_entry_id)
                assert schedule is not None
                assert schedule.status == "completed"

                await bridge.reconcile(publication.id)
                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication.id
                        )
                    )
                ).scalars().all()
                assert len(attempts) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reconcile_task_uses_db_link_not_stale_payload_marker() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                first_item = await ContentRepo(session).create(
                    channel_id=1,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "First"}]
                    ),
                )
                second_item = await ContentRepo(session).create(
                    channel_id=1,
                    document=PostDocument(
                        blocks=[{"id": "b2", "type": "text", "text": "Second"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                first = await bridge.queue(content_item_id=first_item.id)
                second = await bridge.queue(content_item_id=second_item.id)
                second_task = await session.get(PostTask, second.legacy_post_task_id)
                assert second_task is not None

                second_task.status = "processing"
                second_task.payload = {
                    **dict(second_task.payload or {}),
                    "_publication_id": int(first.id),
                }
                await session.commit()

                projected = await bridge.reconcile_task(second_task)
                assert projected is not None
                assert projected.id == second.id
                assert projected.status == "sending"

                first_reloaded = await session.get(Publication, int(first.id))
                assert first_reloaded is not None
                assert first_reloaded.status == "queued"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_bridge_reconciles_scheduler_failure() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=1,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Hello"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                publication = await bridge.queue(content_item_id=item.id)
                task = await session.get(PostTask, publication.legacy_post_task_id)
                assert task is not None
                task.status = "failed"
                task.error = "telegram unavailable"
                await session.commit()

                publication = await bridge.reconcile(publication.id)
                assert publication.status == "failed"
                assert publication.last_error == "telegram unavailable"
                assert publication.attempt_count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_bridge_queues_rich_document_for_shared_renderer() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                document = PostDocument(
                    mode="rich",
                    blocks=[{"id": "p1", "type": "paragraph", "content": "Hello"}],
                )
                item = await ContentRepo(session).create(channel_id=1, document=document)
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id
                )

                task = await session.get(PostTask, publication.legacy_post_task_id)
                assert task is not None
                assert task.payload["type"] == "rich_document"
                assert task.payload["post_document"] == document.to_dict()
                assert task.payload["_publication_id"] == publication.id
        finally:
            await engine.dispose()

    asyncio.run(run())
