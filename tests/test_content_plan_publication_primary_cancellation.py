from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.domain.scheduler import SchedulerTaskLease
from app.services.content_plan_cancellation import ContentPlanCancellationService
from app.services.posting import PostingService


class _Bot:
    pass


async def _linked_fixture(suffix: int):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        owner = Client(
            tg_user_id=9_940_000 + suffix,
            username=f"publicationcancel{suffix}",
            full_name="Publication Cancellation Fixture",
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1_009_940_000_000 - suffix,
            title=f"Publication Cancel {suffix}",
            owner_id=int(owner.id),
        )
        session.add(channel)
        await session.commit()
        channel_id = int(channel.id)

    task = await PostingService(_Bot(), Session).schedule(
        channel_id,
        {"type": "text", "text": f"Publication cancellation {suffix}"},
        datetime.now(timezone.utc) + timedelta(hours=1),
        dedupe_key=f"publication-primary-cancel-{suffix}",
    )
    async with Session() as session:
        publication = (
            await session.execute(
                select(Publication).where(
                    Publication.legacy_post_task_id == int(task.id)
                )
            )
        ).scalar_one()
        return engine, Session, int(task.id), int(publication.id)


def test_publication_identity_is_primary_for_linked_cancellation() -> None:
    async def run() -> None:
        engine, Session, task_id, publication_id = await _linked_fixture(1)
        try:
            result = await ContentPlanCancellationService(Session).delete_publication(
                publication_id
            )
            assert result.outcome == "cancelled"
            assert result.compatibility_retired is True

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None
                assert publication.status == "cancelled"
                assert publication.legacy_post_task_id is None
                assert task is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_identity_without_compatibility_transport_fails_closed() -> None:
    async def run() -> None:
        engine, Session, task_id, publication_id = await _linked_fixture(2)
        try:
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                publication.legacy_post_task_id = None
                await session.commit()

            result = await ContentPlanCancellationService(Session).delete_publication(
                publication_id
            )
            assert result.outcome == "cannot_cancel"
            assert result.reason == "compatibility_transport_absent"
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                publication = await session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.status == "queued"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_identity_preserves_scheduler_lease_barrier() -> None:
    async def run() -> None:
        engine, Session, task_id, publication_id = await _linked_fixture(3)
        try:
            async with Session() as session:
                session.add(
                    SchedulerTaskLease(
                        task_id=task_id,
                        lease_token="publication-primary-cancel-lease",
                        holder="fixture",
                        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                    )
                )
                await session.commit()

            result = await ContentPlanCancellationService(Session).delete_publication(
                publication_id
            )
            assert result.outcome == "cannot_cancel"
            assert result.reason == "legacy_scheduler_lease"
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                publication = await session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.status == "queued"
        finally:
            await engine.dispose()

    asyncio.run(run())
