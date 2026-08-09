from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.repositories.content import ContentRepo
from app.services.document_posting import DocumentPostingService
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import cleanup_runtime_fields, inherit_flags_for_repeat


def test_new_queue_has_no_content_markers_and_repeat_reuses_root() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            when = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)

            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=908,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Queue root"}]
                    ),
                )
                root = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id,
                    scheduled_at=when,
                    repeat_rule={"enabled": True, "seconds": 3600},
                )
                root_task = await session.get(PostTask, int(root.legacy_post_task_id or 0))
                assert root_task is not None
                assert "_publication_id" not in root_task.payload
                assert "_content_item_id" not in root_task.payload
                assert "_content_revision" not in root_task.payload
                assert "_content_channel_id" not in root_task.payload

                child_payload = cleanup_runtime_fields(dict(root_task.payload or {}))
                child_payload = inherit_flags_for_repeat(child_payload, int(root_task.id))
                assert child_payload["repeat_group_id"] == int(root_task.id)
                assert "_content_item_id" not in child_payload
                assert "_content_revision" not in child_payload
                assert "_content_channel_id" not in child_payload

                child = PostTask(
                    channel_id=908,
                    status="pending",
                    scheduled_at=when + timedelta(hours=1),
                    payload=child_payload,
                )
                session.add(child)
                await session.commit()
                await session.refresh(child)

                repeated = await mirror_legacy_post_task(session, child)
                assert repeated is not None
                assert repeated.content_item_id == root.content_item_id
                assert repeated.content_revision == root.content_revision
                assert repeated.meta["repeat_root_provenance"] is True

                await session.refresh(child)
                assert "_content_item_id" not in child.payload
                assert "_content_revision" not in child.payload
                assert "_content_channel_id" not in child.payload

                items = list((await session.execute(select(ContentItem))).scalars().all())
                revisions = list(
                    (await session.execute(select(ContentRevision))).scalars().all()
                )
                assert len(items) == 1
                assert len(revisions) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rich_queue_uses_scheduler_task_context_without_channel_marker() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=909,
                    document=PostDocument(
                        mode="rich",
                        blocks=[{"id": "p1", "type": "paragraph", "content": "Rich queue"}],
                    ),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id
                )
                task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
                assert task is not None
                task_id = int(task.id)
                assert task.payload["type"] == "rich_document"
                assert "_content_channel_id" not in task.payload
                assert "_content_item_id" not in task.payload
                assert "_content_revision" not in task.payload
                scheduler_payload = {
                    **dict(task.payload or {}),
                    "_post_task_id": task_id,
                }

            service = DocumentPostingService(SimpleNamespace(), Session)
            captured: list[int | None] = []

            async def fake_send_document(chat_id, document, *, asset_channel_id=None):
                captured.append(asset_channel_id)
                return [90901]

            service.send_document = fake_send_document  # type: ignore[method-assign]
            result = await service._dispatch(-100909, scheduler_payload)
            assert result == [90901]
            assert captured == [909]
        finally:
            await engine.dispose()

    asyncio.run(run())
