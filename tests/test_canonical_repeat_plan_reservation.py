from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import (
    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY,
    CanonicalRepeatPlanReservationService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.publication_scheduler import Scheduler as PublicationScheduler


async def _seed_published_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=99500 + seed,
            username=f"repeat-reservation-{seed}",
            full_name=f"Repeat Reservation {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10099500 + seed),
            title=f"Repeat Reservation {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Reservation"}]
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
            },
        )
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [99600 + seed],
            "result_link": f"https://t.me/c/{99500 + seed}/{99600 + seed}",
        }
        await session.commit()
        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        assert publication.status == "published"
        return int(publication.id), task_id


async def _counts(session) -> tuple[int, int, int]:
    publications = int((await session.execute(select(func.count(Publication.id)))).scalar_one())
    schedules = int((await session.execute(select(func.count(ScheduleEntry.id)))).scalar_one())
    tasks = int((await session.execute(select(func.count(PostTask.id)))).scalar_one())
    return publications, schedules, tasks


def test_reservation_is_source_only_and_idempotent_after_transport_retirement(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-reservation-idempotent.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_published_repeat(
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
                before = await _counts(session)

                first = await CanonicalRepeatPlanReservationService(session).reserve_next(
                    publication_id,
                    after=source_at + timedelta(minutes=10),
                )
                after_first = await _counts(session)
                second = await CanonicalRepeatPlanReservationService(session).reserve_next(
                    publication_id,
                    after=source_at + timedelta(minutes=10),
                )
                after_second = await _counts(session)

                assert first.outcome == "reserved"
                assert second.outcome == "already_reserved"
                assert before == after_first == after_second

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                publication_snapshot = publication.meta[
                    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY
                ]
                schedule_snapshot = schedule.meta[
                    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY
                ]
                assert publication_snapshot == schedule_snapshot
                assert publication_snapshot["repeat_group_id"] == task_id
                assert publication_snapshot["scheduled_at"] == (
                    source_at + timedelta(hours=1)
                ).isoformat()
                assert publication_snapshot["runtime_options"] == {
                    "autodelete_views": 100,
                    "autodelete_report": True,
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reservation_never_overwrites_a_different_existing_plan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-reservation-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed_published_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                service = CanonicalRepeatPlanReservationService(session)
                first = await service.reserve_next(
                    publication_id,
                    after=source_at + timedelta(minutes=10),
                )
                assert first.outcome == "reserved"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                original = dict(
                    publication.meta[CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY]
                )

                conflict = await service.reserve_next(
                    publication_id,
                    after=source_at + timedelta(hours=2),
                )
                assert conflict.outcome == "conflict"

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert publication.meta[CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY] == original
                assert schedule.meta[CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY] == original
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reservation_does_not_write_when_exact_successor_already_exists(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-reservation-existing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime.now(timezone.utc) + timedelta(hours=2)
            publication_id, task_id = await _seed_published_repeat(
                Session,
                seed=3,
                scheduled_at=source_at,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                await PublicationScheduler(
                    session,
                    object(),
                )._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )
                before = await _counts(session)

                result = await CanonicalRepeatPlanReservationService(session).reserve_next(
                    publication_id,
                    after=source_at + timedelta(minutes=10),
                )
                after = await _counts(session)

                assert result.outcome == "existing_successor"
                assert result.plan is not None
                assert result.plan.existing_publication_id is not None
                assert before == after
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY not in dict(
                    publication.meta or {}
                )
                assert CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY not in dict(
                    schedule.meta or {}
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
