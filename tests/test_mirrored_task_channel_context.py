from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import PostTask
from app.services.document_posting import DocumentPostingService
from app.services.legacy_content_mirror import mirror_legacy_post_task


def test_mirrored_task_drops_channel_marker_but_keeps_db_task_context() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                task = PostTask(
                    channel_id=910,
                    status="pending",
                    payload={
                        "type": "text",
                        "text": "Historical pending task",
                        "_content_channel_id": 910,
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)

                publication = await mirror_legacy_post_task(session, task)
                assert publication is not None
                await session.refresh(task)
                assert "_content_channel_id" not in task.payload

            service = DocumentPostingService(SimpleNamespace(), Session)
            channel_id = await service._task_channel_context({"_post_task_id": task_id})
            assert channel_id == 910
        finally:
            await engine.dispose()

    asyncio.run(run())
