from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.content_plan_publication_links import (
    content_plan_open_callback,
    published_publication_ids_for_legacy_tasks,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session) -> tuple[int, int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=74001,
            username="owner",
            full_name="Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10074001,
            title="Producer",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Published"}]
            ),
            created_by_tg_user_id=74001,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id)
        )
        task_id = int(publication.legacy_post_task_id or 0)
        schedule_id = int(publication.schedule_entry_id or 0)
        publication_id = int(publication.id)
        task = await session.get(PostTask, task_id)
        schedule = await session.get(ScheduleEntry, schedule_id)
        assert task is not None and schedule is not None
        task.status = "done"
        publication.status = "published"
        schedule.status = "completed"
        await session.commit()
        return int(channel.id), task_id, publication_id, schedule_id, int(item.id)


def test_proven_published_link_emits_publication_callback(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'content-plan-links.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            channel_id, task_id, publication_id, _, _ = await _seed(Session)

            async with Session() as session:
                links = await published_publication_ids_for_legacy_tasks(
                    session,
                    channel_id=channel_id,
                    post_task_ids=[task_id],
                )

            assert links == {task_id: publication_id}
            assert content_plan_open_callback(
                post_task_id=task_id,
                date_iso="2026-08-10",
                published_publication_ids=links,
            ) == f"cp_open_pub:{publication_id}:2026-08-10"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_incomplete_canonical_linkage_keeps_legacy_callback(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'content-plan-links-incomplete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            channel_id, task_id, _, schedule_id, _ = await _seed(Session)

            async with Session() as session:
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert schedule is not None
                schedule.status = "pending"
                await session.commit()

                links = await published_publication_ids_for_legacy_tasks(
                    session,
                    channel_id=channel_id,
                    post_task_ids=[task_id],
                )

            assert links == {}
            assert content_plan_open_callback(
                post_task_id=task_id,
                date_iso="2026-08-10",
                published_publication_ids=links,
            ) == f"cp_open_post:{task_id}:2026-08-10"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cross_channel_or_stale_transport_status_is_not_promoted(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'content-plan-links-guards.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            channel_id, task_id, publication_id, schedule_id, _ = await _seed(Session)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "pending"
                await session.commit()
                assert (
                    await published_publication_ids_for_legacy_tasks(
                        session,
                        channel_id=channel_id,
                        post_task_ids=[task_id],
                    )
                    == {}
                )

                task.status = "done"
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert publication is not None and schedule is not None
                publication.channel_id = channel_id + 999
                schedule.channel_id = channel_id + 999
                await session.commit()
                assert (
                    await published_publication_ids_for_legacy_tasks(
                        session,
                        channel_id=channel_id,
                        post_task_ids=[task_id],
                    )
                    == {}
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
