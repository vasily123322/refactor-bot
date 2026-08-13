from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.bot.routers import main_router
from app.bot.routers.content_plan import router as legacy_content_plan_router
from app.bot.routers.content_plan_migration_controls import (
    router as migration_control_router,
)
from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_pending_publication_cancel import (
    CanonicalPendingPublicationCancelService,
)
from app.services.scheduler_task_lease import SchedulerTaskLeaseService


async def _seed(Session, *, seed: int, linked: bool = True):
    now = datetime(2026, 8, 13, 12, 30, tzinfo=timezone.utc)
    async with Session() as session:
        owner = Client(
            tg_user_id=910000 + seed,
            username=f"cancel-{seed}",
            full_name=f"Cancel {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100910000 + seed),
            title=f"Cancel {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        task = PostTask(
            channel_id=int(channel.id),
            status="pending",
            scheduled_at=now + timedelta(minutes=5),
            payload={"type": "text", "text": f"cancel {seed}"},
        )
        session.add(task)
        await session.flush()
        publication_id = None
        schedule_id = None
        if linked:
            schedule = ScheduleEntry(
                content_item_id=1000 + seed,
                content_revision=1,
                channel_id=int(channel.id),
                scheduled_at=task.scheduled_at,
                timezone="UTC",
                status="pending",
                repeat_rule={},
                meta={"legacy_post_task_id": int(task.id)},
            )
            session.add(schedule)
            await session.flush()
            publication = Publication(
                schedule_entry_id=int(schedule.id),
                content_item_id=1000 + seed,
                content_revision=1,
                channel_id=int(channel.id),
                status="queued",
                legacy_post_task_id=int(task.id),
                attempt_count=0,
                meta={},
            )
            session.add(publication)
            await session.flush()
            publication_id = int(publication.id)
            schedule_id = int(schedule.id)
        await session.commit()
        return {
            "user_id": int(owner.tg_user_id),
            "task_id": int(task.id),
            "publication_id": publication_id,
            "schedule_id": schedule_id,
        }


def test_linked_pending_cancel_is_atomic_and_provider_free(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-cancel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1)
            async with Session() as session:
                result = await CanonicalPendingPublicationCancelService(
                    session
                ).cancel_owned_pending(
                    post_task_id=seeded["task_id"],
                    tg_user_id=seeded["user_id"],
                    at=datetime(2026, 8, 13, 12, 31, tzinfo=timezone.utc),
                )
                assert result.outcome == "cancelled"
                assert result.publication_id == seeded["publication_id"]

            async with Session() as session:
                assert await session.get(PostTask, seeded["task_id"]) is None
                publication = await session.get(Publication, seeded["publication_id"])
                schedule = await session.get(ScheduleEntry, seeded["schedule_id"])
                assert publication is not None and publication.status == "cancelled"
                assert publication.legacy_post_task_id is None
                assert schedule is not None and schedule.status == "cancelled"
                assert "legacy_post_task_id" not in dict(schedule.meta or {})
                assert dict(publication.meta or {})["canonical_pending_cancel"][
                    "legacy_post_task_id"
                ] == seeded["task_id"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_scheduler_claim_wins_and_blocks_cancel(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'cancel-contention.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2)
            now = datetime(2026, 8, 13, 12, 31, tzinfo=timezone.utc)
            async with Session() as session:
                lease = await SchedulerTaskLeaseService(session).claim_pending(
                    task_id=seeded["task_id"],
                    holder="legacy-wins",
                    ttl_seconds=120,
                    now=now,
                )
                assert lease is not None
            async with Session() as session:
                result = await CanonicalPendingPublicationCancelService(
                    session
                ).cancel_owned_pending(
                    post_task_id=seeded["task_id"],
                    tg_user_id=seeded["user_id"],
                    at=now + timedelta(seconds=1),
                )
                assert result.outcome == "contention"
            async with Session() as session:
                task = await session.get(PostTask, seeded["task_id"])
                publication = await session.get(Publication, seeded["publication_id"])
                assert task is not None and task.status == "processing"
                assert publication is not None and publication.status == "queued"
                assert publication.legacy_post_task_id == seeded["task_id"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_scheduler_lease_remains_recovery_barrier_after_pending_reset(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'cancel-expired-barrier.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=3)
            now = datetime(2026, 8, 13, 12, 31, tzinfo=timezone.utc)
            async with Session() as session:
                lease = await SchedulerTaskLeaseService(session).claim_pending(
                    task_id=seeded["task_id"],
                    holder="expired-barrier",
                    ttl_seconds=60,
                    now=now - timedelta(minutes=2),
                )
                assert lease is not None
            async with Session() as session:
                task = await session.get(PostTask, seeded["task_id"])
                assert task is not None
                task.status = "pending"
                await session.commit()
            async with Session() as session:
                result = await CanonicalPendingPublicationCancelService(
                    session
                ).cancel_owned_pending(
                    post_task_id=seeded["task_id"],
                    tg_user_id=seeded["user_id"],
                    at=now,
                )
                assert result.outcome == "conflict"
            async with Session() as session:
                task = await session.get(PostTask, seeded["task_id"])
                publication = await session.get(Publication, seeded["publication_id"])
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.status == "queued"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unlinked_legacy_pending_keeps_queue_delete_semantics(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'legacy-cancel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=4, linked=False)
            async with Session() as session:
                result = await CanonicalPendingPublicationCancelService(
                    session
                ).cancel_owned_pending(
                    post_task_id=seeded["task_id"],
                    tg_user_id=seeded["user_id"],
                )
                assert result.outcome == "legacy_deleted"
            async with Session() as session:
                assert await session.get(PostTask, seeded["task_id"]) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pending_cancel_router_precedes_legacy_delete_handler() -> None:
    assert migration_control_router in main_router.sub_routers
    assert legacy_content_plan_router in main_router.sub_routers
    assert main_router.sub_routers.index(
        migration_control_router
    ) < main_router.sub_routers.index(legacy_content_plan_router)
