from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import PostTask
from app.services.scheduler_task_lease import (
    SchedulerTaskLeaseHandle,
    SchedulerTaskLeaseService,
)


def test_scheduler_lease_claim_is_atomic_and_token_guarded(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "scheduler-lease.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 9, 21, 0, tzinfo=timezone.utc)

            async with Session() as session:
                task = PostTask(channel_id=1, status="pending", payload={})
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)

            async with Session() as first_session:
                handle = await SchedulerTaskLeaseService(first_session).claim_pending(
                    task_id=task_id,
                    holder="worker-a",
                    ttl_seconds=60,
                    now=now,
                )
                assert handle is not None
                assert handle.expires_at == now + timedelta(seconds=60)

            async with Session() as second_session:
                assert (
                    await SchedulerTaskLeaseService(second_session).claim_pending(
                        task_id=task_id,
                        holder="worker-b",
                        ttl_seconds=60,
                        now=now,
                    )
                    is None
                )

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                assert task is not None and task.status == "processing"
                assert lease is not None
                assert lease.lease_token == handle.lease_token
                assert lease.holder == "worker-a"

            wrong = SchedulerTaskLeaseHandle(
                task_id=task_id,
                lease_token="wrong-token",
                holder="worker-a",
                expires_at=handle.expires_at,
            )
            async with Session() as wrong_session:
                assert await SchedulerTaskLeaseService(wrong_session).release(wrong) is False

            async with Session() as renew_session:
                renewed = await SchedulerTaskLeaseService(renew_session).renew(
                    handle,
                    ttl_seconds=60,
                    now=now + timedelta(seconds=20),
                )
                assert renewed is not None
                assert renewed.lease_token == handle.lease_token
                assert renewed.expires_at == now + timedelta(seconds=80)

            async with Session() as release_session:
                assert (
                    await SchedulerTaskLeaseService(release_session).release(renewed)
                    is True
                )
                assert await SchedulerTaskLeaseService(release_session).current(task_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_active_lease_rolls_back_a_manual_pending_reset(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "scheduler-active-lease.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 9, 21, 0, tzinfo=timezone.utc)

            async with Session() as session:
                task = PostTask(channel_id=1, status="pending", payload={})
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)
                handle = await SchedulerTaskLeaseService(session).claim_pending(
                    task_id=task_id,
                    holder="worker-a",
                    ttl_seconds=120,
                    now=now,
                )
                assert handle is not None

            # Simulate an unsafe external/manual status reset while the execution
            # lease is still active. The next claim must roll its CAS back.
            async with Session() as reset_session:
                task = await reset_session.get(PostTask, task_id)
                assert task is not None
                task.status = "pending"
                await reset_session.commit()

            async with Session() as contender_session:
                contender = await SchedulerTaskLeaseService(contender_session).claim_pending(
                    task_id=task_id,
                    holder="worker-b",
                    ttl_seconds=120,
                    now=now + timedelta(seconds=10),
                )
                assert contender is None

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                assert task is not None and task.status == "pending"
                assert lease is not None and lease.lease_token == handle.lease_token
        finally:
            await engine.dispose()

    asyncio.run(run())
