from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_autodelete_runtime_planner import (
    CanonicalPublicationAutodeleteRuntimePlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_legacy_published(Session, *, seed: int) -> tuple[int, int]:
    delivered_at = datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc)
    async with Session() as session:
        owner = Client(
            tg_user_id=129000 + seed,
            username=f"autodelete-runtime-owner-{seed}",
            full_name=f"Autodelete Runtime Owner {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(129100 + seed),
            title=f"Autodelete Runtime Owner {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Legacy-backed runtime authority",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=delivered_at - timedelta(minutes=1),
            runtime_options={"autodelete_seconds": 120},
        )
        task_id = int(publication.legacy_post_task_id or 0)
        assert task_id > 0
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        assert schedule is not None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [901]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[901],
                error=None,
                meta={"legacy_post_task_id": task_id},
                finished_at=delivered_at,
            )
        )
        await session.commit()
        return int(publication.id), task_id


def test_legacy_backed_publication_remains_owned_by_runtime_projector(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-runtime-transport-owner.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _task_id = await _seed_legacy_published(Session, seed=1)

            async with Session() as session:
                assert (
                    await CanonicalPublicationAutodeleteRuntimePlanner(session).plan(
                        publication_id
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retention_of_legacy_transport_does_not_reclassify_legacy_attempt_as_canonical(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-runtime-retired-legacy.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_legacy_published(Session, seed=2)

            async with Session() as session:
                from app.domain.publishing.models import Publication

                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

            async with Session() as session:
                assert (
                    await CanonicalPublicationAutodeleteRuntimePlanner(session).plan(
                        publication_id
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
