from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import cleanup_runtime_fields, inherit_flags_for_repeat


def test_repeat_child_reuses_root_publication_without_content_markers() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            when = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=905,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Repeat root"}]
                    ),
                )
                root = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id,
                    scheduled_at=when,
                    repeat_rule={"enabled": True, "seconds": 3600},
                )
                root_task = await session.get(PostTask, int(root.legacy_post_task_id or 0))
                assert root_task is not None

                child_payload = cleanup_runtime_fields(dict(root_task.payload or {}))
                child_payload = inherit_flags_for_repeat(child_payload, int(root_task.id))
                child_payload.pop("_content_item_id", None)
                child_payload.pop("_content_revision", None)
                child = PostTask(
                    channel_id=905,
                    status="pending",
                    scheduled_at=when + timedelta(hours=1),
                    payload=child_payload,
                )
                session.add(child)
                await session.commit()
                await session.refresh(child)

                repeated = await mirror_legacy_post_task(session, child)
                assert repeated is not None
                assert repeated.id != root.id
                assert repeated.content_item_id == root.content_item_id
                assert repeated.content_revision == root.content_revision
                assert repeated.meta["reused_content_provenance"] is True
                assert repeated.meta["repeat_root_provenance"] is True

                schedule = await session.get(
                    ScheduleEntry,
                    int(repeated.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert schedule.meta["repeat_root_provenance"] is True

                items = list((await session.execute(select(ContentItem))).scalars().all())
                revisions = list(
                    (await session.execute(select(ContentRevision))).scalars().all()
                )
                assert len(items) == 1
                assert len(revisions) == 1

                await session.refresh(child)
                assert "_content_item_id" not in child.payload
                assert "_content_revision" not in child.payload
                assert child.payload["_content_channel_id"] == 905
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_root_provenance_never_crosses_channel_boundary() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                root_item = await ContentRepo(session).create(
                    channel_id=906,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Private root"}]
                    ),
                )
                root = await LegacyPublicationBridge(session).queue(
                    content_item_id=root_item.id,
                    repeat_rule={"enabled": True, "seconds": 3600},
                )
                root_task_id = int(root.legacy_post_task_id or 0)
                assert root_task_id > 0

                foreign_child = PostTask(
                    channel_id=907,
                    status="pending",
                    payload={
                        "type": "text",
                        "text": "Own channel fallback",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "repeat_group_id": root_task_id,
                    },
                )
                session.add(foreign_child)
                await session.commit()
                await session.refresh(foreign_child)

                mirrored = await mirror_legacy_post_task(session, foreign_child)
                assert mirrored is not None
                assert mirrored.channel_id == 907
                assert mirrored.content_item_id != root.content_item_id
                assert "repeat_root_provenance" not in dict(mirrored.meta or {})

                own_item = await session.get(ContentItem, int(mirrored.content_item_id))
                assert own_item is not None
                assert own_item.channel_id == 907
                assert own_item.title == "Own channel fallback"
        finally:
            await engine.dispose()

    asyncio.run(run())
