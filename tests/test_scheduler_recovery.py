from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_recovery import (
    UNKNOWN_DELIVERY_ERROR,
    SchedulerTaskRecoveryService,
)
from app.services.scheduler_task_lease import SchedulerTaskLeaseService


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _seed(Session, *, channel_id: int = 97) -> tuple[int, int, int]:
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Recover me"}]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(content_item_id=item.id)
        return (
            int(publication.id),
            int(publication.schedule_entry_id or 0),
            int(publication.legacy_post_task_id or 0),
        )


async def _claim_and_project(Session, task_id: int, *, now: datetime):
    async with Session() as session:
        handle = await SchedulerTaskLeaseService(session).claim_pending(
            task_id=task_id,
            holder="worker-a",
            ttl_seconds=30,
            now=now,
        )
        assert handle is not None

    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None and task.status == "processing"
        publication = await LegacyPublicationBridge(session).reconcile_task(task)
        assert publication is not None
        assert publication.status == "sending"
        assert publication.attempt_count == 1
    return handle


def test_expired_lease_remains_normal_claim_barrier(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'expired-claim-barrier.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)

            async with Session() as session:
                task = PostTask(channel_id=1, status="pending", payload={})
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)
                first = await SchedulerTaskLeaseService(session).claim_pending(
                    task_id=task_id,
                    holder="worker-a",
                    ttl_seconds=30,
                    now=now,
                )
                assert first is not None

            # Simulate an unsafe/manual status reset after the original worker died.
            async with Session() as reset_session:
                task = await reset_session.get(PostTask, task_id)
                assert task is not None
                task.status = "pending"
                await reset_session.commit()

            async with Session() as contender_session:
                second = await SchedulerTaskLeaseService(contender_session).claim_pending(
                    task_id=task_id,
                    holder="worker-b",
                    ttl_seconds=30,
                    now=now + timedelta(seconds=31),
                )
                assert second is None

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                assert task is not None and task.status == "pending"
                assert lease is not None and lease.lease_token == first.lease_token
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_takeover_loses_to_late_live_heartbeat(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'recovery-heartbeat-race.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)

            async with Session() as session:
                task = PostTask(channel_id=1, status="pending", payload={})
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)
                handle = await SchedulerTaskLeaseService(session).claim_pending(
                    task_id=task_id,
                    holder="worker-a",
                    ttl_seconds=30,
                    now=now,
                )
                assert handle is not None

            scan_at = now + timedelta(seconds=31)
            async with Session() as scan_session:
                refs = await SchedulerTaskLeaseService(scan_session).expired(now=scan_at)
                assert len(refs) == 1
                reference = refs[0]

            # The live worker renews after recovery scanned but before takeover CAS.
            async with Session() as heartbeat_session:
                renewed = await SchedulerTaskLeaseService(heartbeat_session).renew(
                    handle,
                    ttl_seconds=60,
                    now=now + timedelta(seconds=20),
                )
                assert renewed is not None

            async with Session() as recovery_session:
                takeover = await SchedulerTaskLeaseService(recovery_session).take_expired(
                    reference,
                    now=scan_at,
                )
                assert takeover is None

            async with Session() as check_session:
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                task = await check_session.get(PostTask, task_id)
                assert lease is not None
                assert lease.lease_token == handle.lease_token
                assert _as_utc(lease.expires_at) > scan_at
                assert task is not None and task.status == "processing"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_lease_with_result_ids_recovers_as_published(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'recovery-confirmed.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
            publication_id, schedule_id, task_id = await _seed(Session)
            await _claim_and_project(Session, task_id, now=now)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [9701, "9702"],
                    "result_link": "https://t.me/example/9702",
                }
                await session.commit()

                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert len(attempts) == 1
                attempt_id = int(attempts[0].id)
                assert attempts[0].status == "sending"
                assert attempts[0].finished_at is None

            tick = await SchedulerTaskRecoveryService(Session).run_once(
                now=now + timedelta(seconds=31)
            )
            assert tick.selected == 1
            assert tick.taken_over == 1
            assert tick.confirmed_published == 1
            assert tick.failed_unknown == 0
            assert tick.failures == 0

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                lease = await SchedulerTaskLeaseService(session).current(task_id)
                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert task is not None and task.status == "done"
                assert task.error is None
                assert task.payload["result_ids"] == [9701, 9702]
                assert lease is None
                assert publication is not None and publication.status == "published"
                assert publication.telegram_message_ids == [9701, 9702]
                assert publication.result_link == "https://t.me/example/9702"
                assert schedule is not None and schedule.status == "completed"
                assert len(attempts) == 1
                assert int(attempts[0].id) == attempt_id
                assert attempts[0].status == "published"
                assert attempts[0].finished_at is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_lease_without_evidence_fails_closed_and_never_requeues(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'recovery-unknown.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
            publication_id, schedule_id, task_id = await _seed(Session, channel_id=98)
            await _claim_and_project(Session, task_id, now=now)

            tick = await SchedulerTaskRecoveryService(Session).run_once(
                now=now + timedelta(seconds=31)
            )
            assert tick.selected == 1
            assert tick.taken_over == 1
            assert tick.confirmed_published == 0
            assert tick.failed_unknown == 1
            assert tick.failures == 0

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                lease = await SchedulerTaskLeaseService(session).current(task_id)
                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert task is not None and task.status == "failed"
                assert task.error == UNKNOWN_DELIVERY_ERROR
                assert task.status != "pending"
                assert lease is None
                assert publication is not None and publication.status == "failed"
                assert publication.last_error == UNKNOWN_DELIVERY_ERROR
                assert schedule is not None and schedule.status == "failed"
                assert len(attempts) == 1
                assert attempts[0].status == "failed"
                assert attempts[0].error == UNKNOWN_DELIVERY_ERROR
                assert attempts[0].finished_at is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_ignores_legacy_processing_rows_without_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'legacy-processing.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                task = PostTask(channel_id=1, status="processing", payload={})
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)

            tick = await SchedulerTaskRecoveryService(Session).run_once()
            assert tick.selected == 0
            assert tick.taken_over == 0

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None and task.status == "processing"
        finally:
            await engine.dispose()

    asyncio.run(run())
