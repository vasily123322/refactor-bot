from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import (
    AUTODELETE_RUNTIME_META_KEY,
    PublicationRuntimeProjector,
)


async def _queue(Session, *, channel_id: int, text: str) -> tuple[int, int]:
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": text}]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id)
        )
        return int(publication.id), int(publication.legacy_post_task_id or 0)


async def _terminalize(
    Session,
    *,
    publication_id: int,
    task_id: int,
    payload_updates: dict,
) -> None:
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        task.status = "done"
        task.payload = {**dict(task.payload or {}), **payload_updates}
        await session.commit()
        publication = await LegacyPublicationBridge(session).reconcile(publication_id)
        assert publication.status == "published"


def test_terminal_runtime_backfill_is_bounded_cursor_based_and_idempotent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'runtime-backfill.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            publication1, task1 = await _queue(Session, channel_id=1001, text="one")
            publication2, task2 = await _queue(Session, channel_id=1002, text="two")
            publication3, task3 = await _queue(Session, channel_id=1003, text="three")
            active_publication, _active_task = await _queue(
                Session, channel_id=1004, text="active"
            )

            await _terminalize(
                Session,
                publication_id=publication1,
                task_id=task1,
                payload_updates={
                    "result_ids": [100101],
                    "autodelete_effective_seconds": 60,
                    "autodelete_at": "2026-08-10T02:00:00Z",
                },
            )
            await _terminalize(
                Session,
                publication_id=publication2,
                task_id=task2,
                payload_updates={"result_ids": [100201]},
            )
            await _terminalize(
                Session,
                publication_id=publication3,
                task_id=task3,
                payload_updates={
                    "result_ids": [100301],
                    "autodelete_effective_seconds": "120",
                    "autodelete_at": "2026-08-10T03:00:00+00:00",
                    "autodeleted": True,
                    "autodeleted_at": "2026-08-10T03:02:00+00:00",
                },
            )

            # Simulate stale historical canonical metadata. A terminal row with no
            # runtime evidence must have the spoofed/stale runtime removed.
            async with Session() as session:
                pub2 = await session.get(Publication, publication2)
                assert pub2 is not None
                pub2.meta = {
                    **dict(pub2.meta or {}),
                    AUTODELETE_RUNTIME_META_KEY: {
                        "deleted": True,
                        "effective_seconds": 999,
                    },
                }
                await session.commit()

            cursor = 0
            total_scanned = 0
            total_updated = 0
            batches = 0
            async with Session() as session:
                projector = PublicationRuntimeProjector(session)
                while True:
                    batch = await projector.backfill_terminal(
                        after_publication_id=cursor,
                        limit=1,
                    )
                    batches += 1
                    total_scanned += batch.scanned
                    total_updated += batch.updated
                    assert batch.next_cursor >= cursor
                    cursor = batch.next_cursor
                    if batch.done:
                        break

            assert batches == 4  # three rows plus one bounded end-of-scan tick
            assert total_scanned == 3
            assert total_updated == 3
            assert cursor == publication3

            async with Session() as session:
                pub1 = await session.get(Publication, publication1)
                pub2 = await session.get(Publication, publication2)
                pub3 = await session.get(Publication, publication3)
                active = await session.get(Publication, active_publication)
                assert pub1 is not None and pub2 is not None and pub3 is not None
                assert active is not None

                assert pub1.status == "published"
                assert pub1.meta[AUTODELETE_RUNTIME_META_KEY] == {
                    "deleted": False,
                    "effective_seconds": 60,
                    "scheduled_at": "2026-08-10T02:00:00+00:00",
                }
                assert AUTODELETE_RUNTIME_META_KEY not in pub2.meta
                assert pub3.meta[AUTODELETE_RUNTIME_META_KEY] == {
                    "deleted": True,
                    "effective_seconds": 120,
                    "scheduled_at": "2026-08-10T03:00:00+00:00",
                    "deleted_at": "2026-08-10T03:02:00+00:00",
                }
                assert active.status == "queued"
                assert AUTODELETE_RUNTIME_META_KEY not in active.meta

                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id.in_(
                                    [publication1, publication2, publication3]
                                )
                            )
                        )
                    ).scalars().all()
                )
                assert len(attempts) == 3
                assert all(attempt.status == "published" for attempt in attempts)
                assert all(attempt.finished_at is not None for attempt in attempts)

                # A restart-style rescan is idempotent: nothing is rewritten.
                batch = await PublicationRuntimeProjector(session).backfill_terminal(
                    after_publication_id=0,
                    limit=100,
                )
                assert batch.scanned == 3
                assert batch.updated == 0
                assert batch.done is True
        finally:
            await engine.dispose()

    asyncio.run(run())
