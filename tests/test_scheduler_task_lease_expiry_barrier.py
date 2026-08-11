from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.models import PostTask
from app.services.scheduler_task_lease import SchedulerTaskLeaseService


def test_scheduler_normal_renew_cannot_revive_expired_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'scheduler-renew-expiry.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

            async with Session() as session:
                task = PostTask(channel_id=1, status="pending", payload={})
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)

                handle = await SchedulerTaskLeaseService(session).claim_pending(
                    task_id=task_id,
                    holder="legacy-worker",
                    ttl_seconds=60,
                    now=now,
                )
                assert handle is not None
                original_expiry = now + timedelta(seconds=60)
                assert handle.expires_at == original_expiry

            async with Session() as live_session:
                renewed = await SchedulerTaskLeaseService(live_session).renew(
                    handle,
                    ttl_seconds=60,
                    now=now + timedelta(seconds=30),
                )
                assert renewed is not None
                assert renewed.expires_at == now + timedelta(seconds=90)
                handle = renewed
                live_expiry = renewed.expires_at

            async with Session() as boundary_session:
                assert (
                    await SchedulerTaskLeaseService(boundary_session).renew(
                        handle,
                        ttl_seconds=60,
                        now=live_expiry,
                    )
                    is None
                )
                stored = await SchedulerTaskLeaseService(boundary_session).current(task_id)
                assert stored is not None
                assert stored.expires_at == live_expiry
                assert stored.lease_token == handle.lease_token

            async with Session() as expired_session:
                assert (
                    await SchedulerTaskLeaseService(expired_session).renew(
                        handle,
                        ttl_seconds=60,
                        now=live_expiry + timedelta(seconds=1),
                    )
                    is None
                )
                stored = await SchedulerTaskLeaseService(expired_session).current(task_id)
                assert stored is not None
                assert stored.expires_at == live_expiry
                assert stored.lease_token == handle.lease_token

            async with Session() as recovery_session:
                refs = await SchedulerTaskLeaseService(recovery_session).expired(
                    now=live_expiry,
                )
                assert len(refs) == 1
                assert refs[0].task_id == task_id
                assert refs[0].lease_token == handle.lease_token
                takeover = await SchedulerTaskLeaseService(recovery_session).take_expired(
                    refs[0],
                    now=live_expiry,
                )
                assert takeover is not None
                assert takeover.lease_token != handle.lease_token

            async with Session() as stale_session:
                assert (
                    await SchedulerTaskLeaseService(stale_session).renew(
                        handle,
                        ttl_seconds=60,
                        now=live_expiry + timedelta(seconds=2),
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
