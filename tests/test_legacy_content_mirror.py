from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.legacy_content_mirror import (
    mirror_legacy_post_task,
    mirror_unlinked_legacy_tasks,
)
from app.services.scheduling import as_utc


def test_mirror_creates_content_schedule_and_publication_idempotently() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                when = datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc)
                task = PostTask(
                    channel_id=42,
                    status="pending",
                    scheduled_at=when,
                    payload={
                        "type": "text",
                        "text": "Legacy post",
                        "repeat_on": True,
                        "repeat_seconds": 7200,
                        "autodelete_at": "runtime-only",
                        "meta": {"author_user_id": 777},
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)

                publication = await mirror_legacy_post_task(session, task)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == task.id

                item = await session.get(ContentItem, publication.content_item_id)
                assert item is not None
                assert item.status == "ready"
                assert item.current_revision == 1
                assert item.title == "Legacy post"

                revision = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == item.id
                        )
                    )
                ).scalar_one()
                assert revision.created_by_tg_user_id == 777
                assert revision.document["blocks"][0]["text"] == "Legacy post"
                extra = revision.document["metadata"].get("legacy_payload_extra", {})
                assert "repeat_seconds" not in extra
                assert "autodelete_at" not in extra

                schedule = await session.get(ScheduleEntry, publication.schedule_entry_id)
                assert schedule is not None
                assert as_utc(schedule.scheduled_at) == when
                assert schedule.repeat_rule == {"enabled": True, "seconds": 7200}

                await session.refresh(task)
                assert task.payload["_content_item_id"] == item.id
                assert task.payload["_content_revision"] == 1
                assert task.payload["_publication_id"] == publication.id

                same = await mirror_legacy_post_task(session, task)
                assert same is not None
                assert same.id == publication.id
                assert len((await session.execute(select(ContentItem))).scalars().all()) == 1
                assert len((await session.execute(select(Publication))).scalars().all()) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mirror_terminal_task_captures_result_once() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                task = PostTask(
                    channel_id=5,
                    status="done",
                    payload={
                        "type": "photo",
                        "file_id": "file-id",
                        "caption": "Published",
                        "result_ids": [10, 11],
                        "result_link": "https://t.me/example/11",
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)

                publication = await mirror_legacy_post_task(session, task)
                assert publication is not None
                assert publication.status == "published"
                assert publication.telegram_message_ids == [10, 11]
                assert publication.result_link == "https://t.me/example/11"
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

                await mirror_legacy_post_task(session, task)
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


def test_batch_mirror_skips_unsupported_payload_without_breaking_task() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                supported = PostTask(
                    channel_id=1,
                    status="pending",
                    payload={"type": "text", "text": "supported"},
                )
                unsupported = PostTask(
                    channel_id=1,
                    status="pending",
                    payload={"type": "media_group", "media": []},
                )
                session.add_all([supported, unsupported])
                await session.commit()

                mirrored, skipped = await mirror_unlinked_legacy_tasks(session)
                assert mirrored == 1
                assert skipped == 1

                await session.refresh(unsupported)
                assert unsupported.status == "pending"
                assert "_publication_id" not in dict(unsupported.payload or {})
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_dispatcher_starts_and_stops_publication_reconciler() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "bot" / "dispatcher.py"
    ).read_text(encoding="utf-8")
    assert "PublicationReconcilerWorker" in source
    assert "await publication_reconciler.start()" in source
    assert 'await _safe_stop("publication reconciler", publication_reconciler.stop)' in source
