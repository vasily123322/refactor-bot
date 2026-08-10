from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.repositories.content import ContentRepo
from app.services.post_task_retention import PostTaskRetentionService
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_terminal(
    Session,
    *,
    channel_id: int,
    status: str,
    payload_updates: dict | None = None,
    repeat: bool = False,
) -> tuple[int, int, int, int]:
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Retention {channel_id}"}]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=item.id,
            repeat_rule={"enabled": True, "seconds": 3600} if repeat else None,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        task.status = status
        if payload_updates:
            task.payload = {**dict(task.payload or {}), **payload_updates}
        await session.commit()
        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == int(publication.id),
                    PublicationAttempt.attempt == int(publication.attempt_count),
                )
            )
        ).scalar_one()
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id or 0))
        assert schedule is not None
        return int(task.id), int(publication.id), int(attempt.id), int(schedule.id)


def test_retention_deletes_only_old_unsuccessful_non_repeat_transport(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'retention-safe.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id, attempt_id, schedule_id = await _seed_terminal(
                Session,
                channel_id=920,
                status="failed",
            )

            async with Session() as session:
                attempt = await session.get(PublicationAttempt, attempt_id)
                assert attempt is not None
                attempt.finished_at = now - timedelta(days=120)
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                ).run_once(now=now)
                assert tick.selected == 1
                assert tick.eligible == 1
                assert tick.deleted == 1
                assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = await session.get(Publication, publication_id)
                attempt = await session.get(PublicationAttempt, attempt_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert publication is not None
                assert publication.status == "failed"
                assert publication.legacy_post_task_id is None
                retention = publication.meta["legacy_transport_retention"]
                assert retention["retired"] is True
                assert retention["terminal_status"] == "failed"
                assert attempt is not None and attempt.status == "failed"
                assert schedule is not None and schedule.status == "failed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retention_skips_repeat_and_delivery_evidence(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'retention-guards.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)

            repeat_task, _, repeat_attempt, _ = await _seed_terminal(
                Session,
                channel_id=921,
                status="failed",
                repeat=True,
            )
            evidence_task, _, evidence_attempt, _ = await _seed_terminal(
                Session,
                channel_id=922,
                status="failed",
                payload_updates={"result_ids": [92201]},
            )

            async with Session() as session:
                for attempt_id in (repeat_attempt, evidence_attempt):
                    attempt = await session.get(PublicationAttempt, attempt_id)
                    assert attempt is not None
                    attempt.finished_at = now - timedelta(days=120)
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                ).run_once(now=now)
                assert tick.selected == 2
                assert tick.deleted == 0
                assert tick.skipped_repeat == 1
                assert tick.skipped_delivery_evidence == 1

            async with Session() as session:
                assert await session.get(PostTask, repeat_task) is not None
                assert await session.get(PostTask, evidence_task) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retention_skips_active_lease_and_recent_or_published_rows(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'retention-age-lease.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)

            leased_task, _, leased_attempt, _ = await _seed_terminal(
                Session,
                channel_id=923,
                status="cancelled",
            )
            recent_task, _, recent_attempt, _ = await _seed_terminal(
                Session,
                channel_id=924,
                status="skipped",
            )
            published_task, _, published_attempt, _ = await _seed_terminal(
                Session,
                channel_id=925,
                status="done",
                payload_updates={"result_ids": [92501]},
            )

            async with Session() as session:
                leased = await session.get(PublicationAttempt, leased_attempt)
                recent = await session.get(PublicationAttempt, recent_attempt)
                published = await session.get(PublicationAttempt, published_attempt)
                assert leased is not None and recent is not None and published is not None
                leased.finished_at = now - timedelta(days=120)
                recent.finished_at = now - timedelta(days=10)
                published.finished_at = now - timedelta(days=120)
                session.add(
                    SchedulerTaskLease(
                        task_id=leased_task,
                        lease_token="retention-active-lease",
                        holder="test-worker",
                        expires_at=now + timedelta(hours=1),
                    )
                )
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                ).run_once(now=now)
                assert tick.selected == 0
                assert tick.deleted == 0

            async with Session() as session:
                assert await session.get(PostTask, leased_task) is not None
                assert await session.get(PostTask, recent_task) is not None
                assert await session.get(PostTask, published_task) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retention_ignores_expired_scheduler_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'retention-expired-lease.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id, attempt_id, _ = await _seed_terminal(
                Session,
                channel_id=926,
                status="failed",
            )

            async with Session() as session:
                attempt = await session.get(PublicationAttempt, attempt_id)
                assert attempt is not None
                attempt.finished_at = now - timedelta(days=120)
                session.add(
                    SchedulerTaskLease(
                        task_id=task_id,
                        lease_token="retention-expired-lease",
                        holder="dead-worker",
                        expires_at=now - timedelta(hours=1),
                    )
                )
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                ).run_once(now=now)
                assert tick.selected == 1
                assert tick.eligible == 1
                assert tick.deleted == 1
                assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                # This engine intentionally does not enable SQLite FK enforcement.
                # Retention must remove the expired lease explicitly rather than rely
                # on ON DELETE CASCADE, otherwise later Alembic adoption would fail
                # its foreign-key integrity audit on an orphan lease row.
                assert await session.get(SchedulerTaskLease, task_id) is None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())
