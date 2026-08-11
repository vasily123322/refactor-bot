from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_recovery_planner import CanonicalRepeatRecoveryPlanner
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import cleanup_runtime_fields, inherit_flags_for_repeat


async def _seed_queued_repeat(Session, *, seed: int, scheduled_at: datetime) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=103000 + seed,
            username=f"repeat-recovery-{seed}",
            full_name=f"Repeat Recovery {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(103100 + seed),
            title=f"Repeat Recovery {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Recovery repeat"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
                "nested": {"mode": "stable"},
            },
        )
        return int(publication.id), int(publication.legacy_post_task_id or 0)


def test_recovery_plan_uses_canonical_state_without_legacy_row(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-no-legacy.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_queued_repeat(
                Session, seed=1, scheduled_at=source_at
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                plan = await CanonicalRepeatRecoveryPlanner(session).plan_recovery(
                    publication_id,
                    after=datetime(2026, 8, 11, 3, 30, tzinfo=timezone.utc),
                )

            assert plan is not None
            assert plan.source_publication_id == publication_id
            assert plan.repeat_group_id == task_id
            assert plan.repeat_seconds == 3600
            assert plan.scheduled_at == datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
            assert plan.runtime_options == {
                "autodelete_views": 100,
                "autodelete_report": True,
                "nested": {"mode": "stable"},
            }
            assert plan.existing_publication_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_plan_rejects_non_overdue_source(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-future.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed_queued_repeat(
                Session, seed=2, scheduled_at=source_at
            )

            async with Session() as session:
                plan = await CanonicalRepeatRecoveryPlanner(session).plan_recovery(
                    publication_id,
                    after=source_at - timedelta(seconds=1),
                )
                assert plan is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_plan_fails_closed_on_attempt_or_runtime_conflict(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            attempt_publication_id, _ = await _seed_queued_repeat(
                Session, seed=3, scheduled_at=source_at
            )
            runtime_publication_id, _ = await _seed_queued_repeat(
                Session, seed=4, scheduled_at=source_at
            )

            async with Session() as session:
                attempt_publication = await session.get(Publication, attempt_publication_id)
                assert attempt_publication is not None
                attempt_publication.attempt_count = 1
                session.add(
                    PublicationAttempt(
                        publication_id=attempt_publication_id,
                        attempt=1,
                        status="sending",
                        meta={},
                    )
                )

                runtime_publication = await session.get(Publication, runtime_publication_id)
                assert runtime_publication is not None
                runtime_schedule = await session.get(
                    ScheduleEntry, int(runtime_publication.schedule_entry_id or 0)
                )
                assert runtime_schedule is not None
                runtime_schedule.meta = {
                    **dict(runtime_schedule.meta or {}),
                    "runtime_options": {"autodelete_views": 999},
                }
                await session.commit()

                planner = CanonicalRepeatRecoveryPlanner(session)
                assert await planner.plan_recovery(
                    attempt_publication_id,
                    after=source_at + timedelta(hours=2),
                ) is None
                assert await planner.plan_recovery(
                    runtime_publication_id,
                    after=source_at + timedelta(hours=2),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_plan_detects_existing_mirrored_child_for_exact_slot(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-existing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_queued_repeat(
                Session, seed=5, scheduled_at=source_at
            )

            async with Session() as session:
                root_task = await session.get(PostTask, task_id)
                assert root_task is not None
                child_payload = inherit_flags_for_repeat(
                    cleanup_runtime_fields(dict(root_task.payload or {})),
                    task_id,
                )
                child = PostTask(
                    channel_id=int(root_task.channel_id),
                    status="pending",
                    scheduled_at=source_at + timedelta(hours=4),
                    payload=child_payload,
                )
                session.add(child)
                await session.commit()
                await session.refresh(child)
                child_publication = await mirror_legacy_post_task(session, child)
                assert child_publication is not None

                plan = await CanonicalRepeatRecoveryPlanner(session).plan_recovery(
                    publication_id,
                    after=source_at + timedelta(hours=3, minutes=30),
                )
                assert plan is not None
                assert plan.scheduled_at == source_at + timedelta(hours=4)
                assert plan.existing_publication_id == int(child_publication.id)
                assert plan.existing_schedule_entry_id == int(
                    child_publication.schedule_entry_id or 0
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
