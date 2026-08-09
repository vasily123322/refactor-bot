from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.legacy_content_mirror import mirror_unlinked_legacy_tasks


def test_legacy_mirror_fails_soft_on_malformed_numeric_metadata() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            oversized = str(1 << 100)

            async with Session() as session:
                task = PostTask(
                    channel_id=903,
                    status="pending",
                    payload={
                        "type": "text",
                        "text": "Keep historical content",
                        "repeat_on": True,
                        "repeat_seconds": "not-a-number",
                        "_publication_id": oversized,
                        "_content_item_id": oversized,
                        "_content_revision": "broken-revision",
                        "_content_channel_id": "broken-channel",
                        "meta": {"author_user_id": oversized},
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)

                mirrored, skipped = await mirror_unlinked_legacy_tasks(session)
                assert mirrored == 1
                assert skipped == 0

                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == task_id
                        )
                    )
                ).scalar_one()
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                revision = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == publication.content_item_id
                        )
                    )
                ).scalar_one()

                assert publication.status == "queued"
                assert schedule is not None
                assert schedule.repeat_rule == {"enabled": False, "seconds": 0}
                assert revision.created_by_tg_user_id is None
                assert revision.document["blocks"][0]["text"] == "Keep historical content"

                await session.refresh(task)
                assert "_publication_id" not in task.payload
                assert task.payload["_content_item_id"] == publication.content_item_id
                assert task.payload["_content_revision"] == publication.content_revision
                assert task.payload["_content_channel_id"] == 903
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_mirror_keeps_valid_numeric_strings() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                task = PostTask(
                    channel_id=904,
                    status="pending",
                    payload={
                        "type": "text",
                        "text": "Valid numeric strings",
                        "repeat_on": True,
                        "repeat_seconds": "3600",
                        "meta": {"author_user_id": "777"},
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)

                mirrored, skipped = await mirror_unlinked_legacy_tasks(session)
                assert mirrored == 1
                assert skipped == 0

                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(task.id)
                        )
                    )
                ).scalar_one()
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                revision = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == publication.content_item_id
                        )
                    )
                ).scalar_one()

                assert schedule is not None
                assert schedule.repeat_rule == {"enabled": True, "seconds": 3600}
                assert revision.created_by_tg_user_id == 777
        finally:
            await engine.dispose()

    asyncio.run(run())
