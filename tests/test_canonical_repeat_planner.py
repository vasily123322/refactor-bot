from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_planner import CanonicalRepeatPlanner
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.publication_scheduler import Scheduler as PublicationScheduler


async def _seed_published_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    repeat_seconds: int = 3600,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=99300 + seed,
            username=f"canonical-repeat-plan-{seed}",
            full_name=f"Canonical Repeat Plan {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10099300 + seed),
            title=f"Canonical Repeat Plan {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Canonical plan"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": repeat_seconds},
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
                "nested": {"mode": "stable"},
            },
        )
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [99400 + seed],
            "result_link": f"https://t.me/c/{99300 + seed}/{99400 + seed}",
        }
        await session.commit()
        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        assert publication.status == "published"
        return int(publication.id), task_id, int(channel.id)


def test_plan_next_uses_only_canonical_state_after_root_transport_retirement(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-plan-retired.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id, _ = await _seed_published_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                plan = await CanonicalRepeatPlanner(session).plan_next(
                    publication_id,
                    after=source_at + timedelta(minutes=10),
                )

            assert plan is not None
            assert plan.source_publication_id == publication_id
            assert plan.repeat_group_id == task_id
            assert plan.repeat_seconds == 3600
            assert plan.scheduled_at == source_at + timedelta(hours=1)
            assert plan.runtime_options == {
                "autodelete_views": 100,
                "autodelete_report": True,
                "nested": {"mode": "stable"},
            }
            assert plan.existing_publication_id is None
            assert plan.existing_schedule_entry_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_plan_next_matches_legacy_catchup_timing(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-plan-catchup.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed_published_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                plan = await CanonicalRepeatPlanner(session).plan_next(
                    publication_id,
                    after=datetime(2026, 8, 11, 3, 30, tzinfo=timezone.utc),
                )

            assert plan is not None
            assert plan.scheduled_at == datetime(
                2026,
                8,
                11,
                4,
                0,
                tzinfo=timezone.utc,
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_plan_next_detects_existing_mirrored_legacy_successor_after_root_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-plan-existing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime.now(timezone.utc) + timedelta(hours=2)
            publication_id, task_id, _ = await _seed_published_repeat(
                Session,
                seed=3,
                scheduled_at=source_at,
            )

            async with Session() as session:
                root_task = await session.get(PostTask, task_id)
                root_publication = await session.get(Publication, publication_id)
                assert root_task is not None and root_publication is not None
                await PublicationScheduler(
                    session,
                    object(),
                )._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    root_task,
                    dict(root_task.payload or {}),
                )

                root_publication = await session.get(Publication, publication_id)
                root_task = await session.get(PostTask, task_id)
                assert root_publication is not None and root_task is not None
                root_publication.legacy_post_task_id = None
                await session.delete(root_task)
                await session.commit()

                plan = await CanonicalRepeatPlanner(session).plan_next(
                    publication_id,
                    after=source_at + timedelta(minutes=10),
                )
                assert plan is not None
                assert plan.existing_publication_id is not None
                assert plan.existing_schedule_entry_id is not None

                successor = await session.get(
                    Publication,
                    int(plan.existing_publication_id),
                )
                successor_schedule = await session.get(
                    ScheduleEntry,
                    int(plan.existing_schedule_entry_id),
                )
                assert successor is not None and successor_schedule is not None
                assert successor.content_item_id == plan.content_item_id
                assert successor.content_revision == plan.content_revision
                assert successor.meta["repeat_group_id"] == task_id
                assert successor_schedule.meta["repeat_group_id"] == task_id
                assert successor_schedule.scheduled_at == plan.scheduled_at.replace(
                    tzinfo=None
                ) or successor_schedule.scheduled_at == plan.scheduled_at
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_plan_next_fails_closed_on_canonical_runtime_intent_conflict(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-plan-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed_published_repeat(
                Session,
                seed=4,
                scheduled_at=source_at,
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": {"autodelete_views": 999},
                }
                await session.commit()

                plan = await CanonicalRepeatPlanner(session).plan_next(
                    publication_id,
                    after=source_at + timedelta(minutes=10),
                )
                assert plan is None
        finally:
            await engine.dispose()

    asyncio.run(run())
