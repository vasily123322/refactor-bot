from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.content import LegacyPayloadError, legacy_payload_from_document
from app.services.legacy_content_mirror import (
    mirror_legacy_post_task,
    mirror_unlinked_legacy_tasks,
)
from app.services.scheduling import (
    as_utc,
    cleanup_runtime_fields,
    inherit_flags_for_repeat,
)


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
                assert "_content_item_id" not in task.payload
                assert "_content_revision" not in task.payload
                assert task.payload["_content_channel_id"] == 42
                assert "_publication_id" not in task.payload

                same = await mirror_legacy_post_task(session, task)
                assert same is not None
                assert same.id == publication.id
                assert len((await session.execute(select(ContentItem))).scalars().all()) == 1
                assert len((await session.execute(select(Publication))).scalars().all()) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_child_reuses_content_but_gets_distinct_publication() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                when = datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc)
                parent = PostTask(
                    channel_id=42,
                    status="pending",
                    scheduled_at=when,
                    payload={
                        "type": "text",
                        "text": "Repeat me",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                    },
                )
                session.add(parent)
                await session.commit()
                await session.refresh(parent)

                first = await mirror_legacy_post_task(session, parent)
                assert first is not None
                await session.refresh(parent)

                child_payload = cleanup_runtime_fields(dict(parent.payload or {}))
                child_payload = inherit_flags_for_repeat(child_payload, int(parent.id))
                assert "_content_item_id" not in child_payload
                assert "_content_revision" not in child_payload
                assert child_payload["_content_channel_id"] == 42
                assert child_payload["repeat_group_id"] == parent.id
                assert "_publication_id" not in child_payload

                # Simulate an older child carrying stale parent publication identity;
                # DB repeat-root provenance must win and the stale marker is removed.
                child_payload["_publication_id"] = int(first.id)
                child = PostTask(
                    channel_id=42,
                    status="pending",
                    scheduled_at=when + timedelta(hours=1),
                    payload=child_payload,
                )
                session.add(child)
                await session.commit()
                await session.refresh(child)

                second = await mirror_legacy_post_task(session, child)
                assert second is not None
                assert second.id != first.id
                assert second.legacy_post_task_id == child.id
                assert second.content_item_id == first.content_item_id
                assert second.content_revision == first.content_revision
                assert second.meta["reused_content_provenance"] is True
                assert second.meta["repeat_root_provenance"] is True

                await session.refresh(child)
                assert "_publication_id" not in child.payload
                assert "_content_item_id" not in child.payload
                assert "_content_revision" not in child.payload
                assert child.payload["_content_channel_id"] == 42

                items = (await session.execute(select(ContentItem))).scalars().all()
                revisions = (await session.execute(select(ContentRevision))).scalars().all()
                publications = (await session.execute(select(Publication))).scalars().all()
                schedules = (await session.execute(select(ScheduleEntry))).scalars().all()
                assert len(items) == 1
                assert len(revisions) == 1
                assert len(publications) == 2
                assert len(schedules) == 2
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


def test_batch_mirror_preserves_unknown_payload_as_opaque_content() -> None:
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
                unknown = PostTask(
                    channel_id=1,
                    status="pending",
                    payload={
                        "type": "future_media_type",
                        "text": "opaque but searchable",
                        "future_field": {"keep": True},
                    },
                )
                session.add_all([supported, unknown])
                await session.commit()

                mirrored, skipped = await mirror_unlinked_legacy_tasks(session)
                assert mirrored == 2
                assert skipped == 0

                await session.refresh(unknown)
                assert "_publication_id" not in unknown.payload
                assert "_content_item_id" not in unknown.payload
                assert "_content_revision" not in unknown.payload
                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(unknown.id)
                        )
                    )
                ).scalar_one()
                revision = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id
                            == publication.content_item_id
                        )
                    )
                ).scalar_one()
                block = revision.document["blocks"][0]
                assert block["type"] == "legacy"
                assert block["legacy_type"] == "future_media_type"
                assert block["payload"]["future_field"] == {"keep": True}

                # Opaque content is migratable/readable but cannot silently publish.
                with pytest.raises(LegacyPayloadError, match="requires a renderer"):
                    legacy_payload_from_document(revision.document)
        finally:
            await engine.dispose()

    import pytest

    asyncio.run(run())


def test_dispatcher_starts_and_stops_publication_reconciler() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "bot" / "dispatcher.py"
    ).read_text(encoding="utf-8")
    assert "PublicationReconcilerWorker" in source
    assert "await publication_reconciler.start()" in source
    assert 'await _safe_stop("publication reconciler", publication_reconciler.stop)' in source