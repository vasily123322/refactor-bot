from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.post_task_retention import PostTaskRetentionService
from app.services.publication_bridge import LegacyPublicationBridge


def test_successful_execution_keeps_current_compatibility_link(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retention-success.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=930,
                    document=PostDocument(blocks=[{"id": "b1", "type": "text", "text": "Retention"}]),
                )
                publication = await LegacyPublicationBridge(session).queue(content_item_id=item.id)
                task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
                assert task is not None
                task.status = "done"
                task.payload = {**dict(task.payload or {}), "result_ids": [93001]}
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
                attempt.finished_at = now - timedelta(days=120)
                task_id = int(task.id)
                publication_id = int(publication.id)
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    retire_successful=True,
                ).run_once(now=now)
                assert tick.selected == 1
                assert tick.deleted == 0
                assert tick.skipped_canonical_delivery == 0
                assert tick.skipped_content_linkage == 1

            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())
