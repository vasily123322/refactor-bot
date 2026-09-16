from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseService
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session, *, seed_id: int, unlink: bool, published: bool) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=79000 + seed_id,
            username=f"lease-owner-{seed_id}",
            full_name=f"Lease Owner {seed_id}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10079000 + seed_id),
            title=f"Lease channel {seed_id}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Lease content"}]
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
                dedupe_key=f"test-autodelete-lease-compat:{int(publication.id)}",
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
        if unlink and task is not None:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_publication_autodelete_lease_is_exclusive_reclaimable_and_token_safe(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-autodelete-lease.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(
                Session, seed_id=1, unlink=True, published=True
            )
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                first = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="worker-a",
                    ttl_seconds=60,
                    now=now,
                )
            assert first is not None
            assert first.publication_id == publication_id

            async with Session() as session:
                busy = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="worker-b",
                    ttl_seconds=60,
                    now=now + timedelta(seconds=30),
                )
            assert busy is None

            async with Session() as session:
                second = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="worker-b",
                    ttl_seconds=90,
                    now=now + timedelta(seconds=61),
                )
            assert second is not None
            assert second.lease_token != first.lease_token

            async with Session() as session:
                stale_release = await PublicationAutodeleteLeaseService(session).release(
                    first
                )
            assert stale_release is False

            async with Session() as session:
                renewed = await PublicationAutodeleteLeaseService(session).renew(
                    second,
                    ttl_seconds=120,
                    now=now + timedelta(seconds=70),
                )
            assert renewed is not None
            assert renewed.lease_token == second.lease_token
            assert renewed.expires_at == now + timedelta(seconds=190)

            async with Session() as session:
                current = await PublicationAutodeleteLeaseService(session).current(
                    publication_id
                )
                assert current is not None
                assert current.lease_token == second.lease_token

            async with Session() as session:
                released = await PublicationAutodeleteLeaseService(session).release(renewed)
            assert released is True

            async with Session() as session:
                assert (
                    await PublicationAutodeleteLeaseService(session).current(publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_autodelete_lease_rejects_linked_or_nonpublished_rows(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-autodelete-lease-guards.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            linked_id = await _seed(Session, seed_id=1, unlink=False, published=True)
            queued_id = await _seed(Session, seed_id=2, unlink=True, published=False)

            async with Session() as session:
                service = PublicationAutodeleteLeaseService(session)
                linked = await service.acquire(
                    publication_id=linked_id,
                    holder="worker",
                )
                queued = await service.acquire(
                    publication_id=queued_id,
                    holder="worker",
                )

            assert linked is None
            assert queued is None
        finally:
            await engine.dispose()

    asyncio.run(run())
