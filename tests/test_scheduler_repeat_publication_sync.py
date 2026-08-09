from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.workers.publication_scheduler as scheduler_module
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.publication_scheduler import Scheduler


async def _seed_repeat(Session, *, channel_id: int, scheduled_at: datetime):
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Repeat root"}]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=item.id,
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
        )
        return int(publication.id), int(publication.legacy_post_task_id or 0)


def test_normal_repeat_child_gets_publication_immediately(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'normal-repeat-sync.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            when = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
            root_publication_id, root_task_id = await _seed_repeat(
                Session,
                channel_id=912,
                scheduled_at=when,
            )
            scheduler = Scheduler(Session, SimpleNamespace())

            async with Session() as session:
                root = await session.get(PostTask, root_task_id)
                assert root is not None
                await scheduler._schedule_next_repeat_if_needed(
                    session,
                    root,
                    dict(root.payload or {}),
                )

            async with Session() as session:
                tasks = list(
                    (
                        await session.execute(
                            select(PostTask).order_by(PostTask.id.asc())
                        )
                    ).scalars().all()
                )
                assert len(tasks) == 2
                child = next(task for task in tasks if int(task.id) != root_task_id)
                assert child.status == "pending"
                assert child.payload["repeat_group_id"] == root_task_id
                assert not any(
                    key in child.payload
                    for key in (
                        "_publication_id",
                        "_content_item_id",
                        "_content_revision",
                        "_content_channel_id",
                    )
                )

                root_publication = await session.get(Publication, root_publication_id)
                child_publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(child.id)
                        )
                    )
                ).scalar_one_or_none()
                assert root_publication is not None
                assert child_publication is not None
                assert child_publication.status == "queued"
                assert child_publication.content_item_id == root_publication.content_item_id
                assert child_publication.content_revision == root_publication.content_revision
                assert child_publication.meta["repeat_root_provenance"] is True

                items = list((await session.execute(select(ContentItem))).scalars().all())
                revisions = list(
                    (await session.execute(select(ContentRevision))).scalars().all()
                )
                assert len(items) == 1
                assert len(revisions) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_boot_cleanup_projects_skipped_parent_and_mirrors_child_immediately(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'boot-repeat-sync.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
            boot_time = scheduled_at + timedelta(hours=5)
            root_publication_id, root_task_id = await _seed_repeat(
                Session,
                channel_id=913,
                scheduled_at=scheduled_at,
            )
            scheduler = Scheduler(Session, SimpleNamespace())
            scheduler._boot_time = boot_time

            async with Session() as session:
                root = await session.get(PostTask, root_task_id)
                assert root is not None
                remaining = await scheduler._boot_cleanup_repeats(session, [root])
                assert remaining == []

            async with Session() as session:
                root = await session.get(PostTask, root_task_id)
                root_publication = await session.get(Publication, root_publication_id)
                assert root is not None and root.status == "skipped"
                assert root_publication is not None
                assert root_publication.status == "skipped"

                child = (
                    await session.execute(
                        select(PostTask).where(PostTask.id != root_task_id)
                    )
                ).scalar_one()
                child_publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(child.id)
                        )
                    )
                ).scalar_one_or_none()
                assert child.status == "pending"
                assert child_publication is not None
                assert child_publication.status == "queued"
                assert child_publication.content_item_id == root_publication.content_item_id
                assert child_publication.content_revision == root_publication.content_revision
                assert child_publication.meta["repeat_root_provenance"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_mirror_failure_keeps_committed_child_for_reconciler(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-mirror-recovery.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            when = datetime(2026, 8, 10, 19, 0, tzinfo=timezone.utc)
            root_publication_id, root_task_id = await _seed_repeat(
                Session,
                channel_id=914,
                scheduled_at=when,
            )
            scheduler = Scheduler(Session, SimpleNamespace())

            real_mirror = scheduler_module.mirror_legacy_post_task

            async def fail_child_mirror(session, task):
                if int(task.id) != root_task_id:
                    raise RuntimeError("simulated mirror failure")
                return await real_mirror(session, task)

            monkeypatch.setattr(
                scheduler_module,
                "mirror_legacy_post_task",
                fail_child_mirror,
            )

            async with Session() as session:
                root = await session.get(PostTask, root_task_id)
                assert root is not None
                await scheduler._schedule_next_repeat_if_needed(
                    session,
                    root,
                    dict(root.payload or {}),
                )

            async with Session() as session:
                root_publication = await session.get(Publication, root_publication_id)
                assert root_publication is not None
                child = (
                    await session.execute(
                        select(PostTask).where(PostTask.id != root_task_id)
                    )
                ).scalar_one()
                assert child.status == "pending"
                assert (
                    await session.execute(
                        select(Publication.id).where(
                            Publication.legacy_post_task_id == int(child.id)
                        )
                    )
                ).scalar_one_or_none() is None
        finally:
            await engine.dispose()

    asyncio.run(run())
