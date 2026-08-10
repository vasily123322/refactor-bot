from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content.models import ContentItem
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.scheduling import cleanup_runtime_fields, inherit_flags_for_repeat


def test_repeat_root_persists_canonical_group_anchor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-root-anchor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                root = PostTask(
                    channel_id=42,
                    status="done",
                    scheduled_at=datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc),
                    payload={
                        "type": "text",
                        "text": "Canonical repeat root",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "result_ids": [42001],
                    },
                )
                session.add(root)
                await session.commit()
                await session.refresh(root)

                publication = await mirror_legacy_post_task(session, root)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert publication.meta["repeat_group_id"] == int(root.id)
                assert schedule.meta["repeat_group_id"] == int(root.id)
                assert schedule.repeat_rule == {"enabled": True, "seconds": 3600}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_child_reuses_canonical_root_after_root_transport_deleted(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-root-retired.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                when = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
                root = PostTask(
                    channel_id=43,
                    status="done",
                    scheduled_at=when,
                    payload={
                        "type": "text",
                        "text": "Immutable root content",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "result_ids": [43001],
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

                # Simulate safe terminal root transport retirement. Canonical group
                # metadata must remain sufficient for future repeat child provenance.
                root_publication.legacy_post_task_id = None
                await session.delete(root)
                await session.commit()

                retained_root = await session.get(Publication, root_publication_id)
                assert retained_root is not None
                assert retained_root.legacy_post_task_id is None
                assert retained_root.meta["repeat_group_id"] == root_task_id

                child_payload = cleanup_runtime_fields(
                    {
                        "type": "text",
                        "text": "Child transport text must not become new content",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                    }
                )
                child_payload = inherit_flags_for_repeat(child_payload, root_task_id)
                child = PostTask(
                    channel_id=43,
                    status="pending",
                    scheduled_at=when + timedelta(hours=1),
                    payload=child_payload,
                )
                session.add(child)
                await session.commit()
                await session.refresh(child)

                child_publication = await mirror_legacy_post_task(session, child)
                assert child_publication is not None
                child_schedule = await session.get(
                    ScheduleEntry,
                    int(child_publication.schedule_entry_id or 0),
                )
                assert child_schedule is not None
                assert child_publication.content_item_id == root_content_item_id
                assert child_publication.content_revision == root_content_revision
                assert child_publication.meta["repeat_group_id"] == root_task_id
                assert child_schedule.meta["repeat_group_id"] == root_task_id
                assert child_publication.meta["repeat_root_provenance"] is True
                assert child_schedule.meta["repeat_root_provenance"] is True

                # No second ContentItem was created from the child's mutable payload.
                items = (await session.execute(__import__("sqlalchemy").select(ContentItem))).scalars().all()
                assert len(items) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_group_lookup_is_channel_scoped(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-group-channel-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                when = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
                root = PostTask(
                    channel_id=50,
                    status="done",
                    scheduled_at=when,
                    payload={
                        "type": "text",
                        "text": "Channel 50 root",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "result_ids": [50001],
                    },
                )
                session.add(root)
                await session.commit()
                await session.refresh(root)
                root_task_id = int(root.id)
                root_publication = await mirror_legacy_post_task(session, root)
                assert root_publication is not None
                root_content_item_id = int(root_publication.content_item_id)
                root_publication.legacy_post_task_id = None
                await session.delete(root)
                await session.commit()

                foreign_child = PostTask(
                    channel_id=51,
                    status="pending",
                    scheduled_at=when + timedelta(hours=1),
                    payload={
                        "type": "text",
                        "text": "Channel 51 owns separate content",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "repeat_group_id": root_task_id,
                    },
                )
                session.add(foreign_child)
                await session.commit()
                await session.refresh(foreign_child)

                publication = await mirror_legacy_post_task(session, foreign_child)
                assert publication is not None
                assert publication.channel_id == 51
                assert publication.content_item_id != root_content_item_id
                assert publication.meta.get("repeat_root_provenance") is not True
                assert publication.meta["repeat_group_id"] == root_task_id
        finally:
            await engine.dispose()

    asyncio.run(run())
