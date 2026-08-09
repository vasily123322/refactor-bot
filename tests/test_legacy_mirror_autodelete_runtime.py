from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import PostTask
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


def test_terminal_legacy_mirror_preserves_sanitized_autodelete_runtime() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                task = PostTask(
                    channel_id=903,
                    status="done",
                    scheduled_at=datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
                    payload={
                        "type": "text",
                        "text": "mirrored autodelete",
                        "result_ids": [90301],
                        "autodelete_seconds": 300,
                        "autodelete_effective_seconds": "420",
                        "autodelete_at": "2026-08-10T01:07:00Z",
                        "autodeleted": True,
                        "autodeleted_at": "2026-08-10T01:07:03+00:00",
                        "provider_secret": "must-not-copy",
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)

                publication = await mirror_legacy_post_task(session, task)
                assert publication is not None
                assert publication.status == "published"
                assert publication.meta["mirrored_from_legacy"] is True
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY] == {
                    "deleted": True,
                    "effective_seconds": 420,
                    "scheduled_at": "2026-08-10T01:07:00+00:00",
                    "deleted_at": "2026-08-10T01:07:03+00:00",
                }
                serialized_meta = repr(publication.meta)
                assert "provider_secret" not in serialized_meta
                assert "must-not-copy" not in serialized_meta
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_mirror_does_not_invent_autodelete_runtime_from_malformed_fields() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                task = PostTask(
                    channel_id=904,
                    status="failed",
                    error="telegram unavailable",
                    scheduled_at=datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
                    payload={
                        "type": "text",
                        "text": "malformed runtime",
                        "autodelete_seconds": -1,
                        "autodelete_effective_seconds": True,
                        "autodelete_at": "not-a-date",
                        "autodeleted": "true",
                        "autodeleted_at": "javascript:alert(1)",
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)

                publication = await mirror_legacy_post_task(session, task)
                assert publication is not None
                assert publication.status == "failed"
                assert publication.last_error == "telegram unavailable"
                assert AUTODELETE_RUNTIME_META_KEY not in publication.meta
        finally:
            await engine.dispose()

    asyncio.run(run())
