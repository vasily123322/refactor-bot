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
from app.services.post_task_retention import PostTaskRetentionService, _RepeatHandoff
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(
    Session,
    *,
    channel_id: int,
    now: datetime,
    repeat: bool = False,
    payload_updates: dict | None = None,
) -> tuple[int, int]:
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Retention {channel_id}"}]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=item.id,
            repeat_rule={"enabled": True, "seconds": 3600} if repeat else None,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [channel_id * 100 + 1],
            **dict(payload_updates or {}),
        }
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
        if channel_id in {932, 933}:
            publication.meta = {
                **dict(publication.meta or {}),
                "runtime_options": (
                    {"autodelete_seconds": 60}
                    if channel_id == 932
                    else {"autodelete_seconds": 60, "autodelete_views": 10}
                ),
            }
        await session.commit()
        return int(task.id), int(publication.id)


def test_advanced_proofs_keep_link_and_time_views_fallback(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'retention-advanced-proofs.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            seeded = [
                await _seed(Session, channel_id=931, now=now, repeat=True),
                await _seed(
                    Session,
                    channel_id=932,
                    now=now,
                    payload_updates={"autodelete_seconds": 60},
                ),
                await _seed(
                    Session,
                    channel_id=933,
                    now=now,
                    payload_updates={"autodelete_seconds": 60, "autodelete_views": 10},
                ),
            ]

            async with Session() as session:
                service = PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_pending_autodelete=True,
                    retire_successful_repeat_occurrences=True,
                )

                async def prove_successor(*, task, **_kwargs):
                    return _RepeatHandoff(
                        group_id=int(task.id),
                        successor_task_id=int(task.id) + 1000,
                    )

                async def prove_pending(*, payload, **_kwargs):
                    return not bool(payload.get("autodelete_views"))

                monkeypatch.setattr(service, "_successful_repeat_handoff", prove_successor)
                monkeypatch.setattr(service, "_pending_autodelete_is_canonical", prove_pending)
                tick = await service.run_once(now=now)
                assert tick.selected == 3
                assert tick.eligible == 0
                assert tick.deleted == 0
                assert tick.skipped_repeat == 0
                assert tick.skipped_canonical_delivery == 0
                assert tick.skipped_content_linkage == 2
                assert tick.skipped_pending_autodelete == 1

            async with Session() as session:
                for task_id, publication_id in seeded:
                    assert await session.get(PostTask, task_id) is not None
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    assert publication.legacy_post_task_id == task_id
                    assert "legacy_transport_retention" not in dict(publication.meta or {})
        finally:
            await engine.dispose()

    asyncio.run(run())
