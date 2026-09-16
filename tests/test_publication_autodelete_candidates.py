from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_candidates import (
    PublicationAutodeleteCandidateSelector,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(
    Session,
    *,
    seed_id: int,
    unlink: bool = True,
    published: bool = True,
    corrupt_schedule_channel: bool = False,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=79500 + seed_id,
            username=f"candidate-owner-{seed_id}",
            full_name=f"Candidate Owner {seed_id}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10079500 + seed_id),
            title=f"Candidate channel {seed_id}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Row {seed_id}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id)
        )
        schedule = await session.get(
            ScheduleEntry, int(publication.schedule_entry_id or 0)
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert schedule is not None

        if task is None and not unlink:
            task = PostTask(
                channel_id=int(channel.id),
                status="pending",
                payload={},
                dedupe_key=f"test-candidate-compat:{int(publication.id)}",
                scheduled_at=schedule.scheduled_at,
            )
            session.add(task)
            await session.flush()
            publication.legacy_post_task_id = int(task.id)

        if published:
            publication.status = "published"
            schedule.status = "completed"
            if task is not None:
                task.status = "done"
        if corrupt_schedule_channel:
            schedule.channel_id = int(channel.id) + 10000
        if unlink and task is not None:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_candidate_selector_is_bounded_cursor_stable_and_canonical_only(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-autodelete-candidates.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            first_id = await _seed(Session, seed_id=1)
            linked_id = await _seed(Session, seed_id=2, unlink=False)
            second_id = await _seed(Session, seed_id=3)
            queued_id = await _seed(Session, seed_id=4, published=False)
            third_id = await _seed(Session, seed_id=5)
            corrupt_id = await _seed(
                Session,
                seed_id=6,
                corrupt_schedule_channel=True,
            )

            async with Session() as session:
                selector = PublicationAutodeleteCandidateSelector(session)
                first = await selector.select_batch(limit=2)
                second = await selector.select_batch(
                    after_publication_id=first.next_cursor,
                    limit=2,
                )

            assert first.publication_ids == (first_id, second_id)
            assert first.next_cursor == second_id
            assert first.done is False
            assert second.publication_ids == (third_id,)
            assert second.next_cursor == third_id
            assert second.done is True
            assert linked_id not in first.publication_ids + second.publication_ids
            assert queued_id not in first.publication_ids + second.publication_ids
            assert corrupt_id not in first.publication_ids + second.publication_ids
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_candidate_selector_clamps_invalid_cursor_and_limit(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-autodelete-candidate-bounds.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(Session, seed_id=1)

            async with Session() as session:
                batch = await PublicationAutodeleteCandidateSelector(session).select_batch(
                    after_publication_id="invalid",  # type: ignore[arg-type]
                    limit=0,
                )

            assert batch.publication_ids == (publication_id,)
            assert batch.next_cursor == publication_id
            assert batch.done is False
        finally:
            await engine.dispose()

    asyncio.run(run())
