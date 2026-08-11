from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import (
    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY,
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.canonical_repeat_transport_adapter import CanonicalRepeatTransportAdapter
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed_terminal_canonical_repeat(Session, *, seed: int, now: datetime) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=212000 + seed,
            username=f"repeat-race-{seed}",
            full_name=f"Repeat Race {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100212000 + seed),
            title=f"Repeat Race {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"repeat race {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"silent": True},
        )
        assert publication.legacy_post_task_id is not None
        task = await session.get(PostTask, int(publication.legacy_post_task_id))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [8100 + seed]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[8100 + seed],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=now - timedelta(minutes=1),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


async def _relink_legacy_transport(Session, source_id: int) -> int:
    async with Session() as session:
        source = await session.get(Publication, source_id)
        assert source is not None
        schedule = await session.get(ScheduleEntry, int(source.schedule_entry_id))
        assert schedule is not None
        task = PostTask(
            channel_id=int(source.channel_id),
            status="pending",
            payload={
                "repeat_on": True,
                "repeat_seconds": 60,
                "silent": True,
            },
            dedupe_key=f"repeat-race-relink:{source_id}",
            scheduled_at=schedule.scheduled_at,
        )
        session.add(task)
        await session.flush()
        task_id = int(task.id)
        source.legacy_post_task_id = task_id
        schedule.meta = {
            **dict(schedule.meta or {}),
            "legacy_post_task_id": task_id,
        }
        await session.commit()
        return task_id


async def _successor_count(Session, source_id: int) -> int:
    async with Session() as session:
        source = await session.get(Publication, source_id)
        assert source is not None
        rows = (
            await session.execute(
                select(Publication).where(
                    Publication.id != source_id,
                    Publication.content_item_id == int(source.content_item_id),
                    Publication.content_revision == int(source.content_revision),
                    Publication.channel_id == int(source.channel_id),
                )
            )
        ).scalars().all()
        return len(rows)


def test_select_then_relink_makes_reserve_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-select-relink-reserve.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_terminal_canonical_repeat(Session, seed=1, now=now)
            worker = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )

            selected, _, _, _ = await worker._select()
            assert selected == [source_id]
            task_id = await _relink_legacy_transport(Session, source_id)

            async with Session() as session:
                reservation = await CanonicalRepeatPlanReservationService(
                    session
                ).reserve_next(source_id, after=now)
                assert reservation.outcome == "ineligible"

            async with Session() as session:
                source = await session.get(Publication, source_id)
                task = await session.get(PostTask, task_id)
                schedule = await session.get(ScheduleEntry, int(source.schedule_entry_id))
                assert source is not None and schedule is not None
                assert source.legacy_post_task_id == task_id
                assert task is not None and task.status == "pending"
                assert CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY not in dict(
                    source.meta or {}
                )
                assert CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY not in dict(
                    schedule.meta or {}
                )
            assert await _successor_count(Session, source_id) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reservation_then_relink_blocks_verifier_and_materialize_and_replay(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-reserve-relink-materialize.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_terminal_canonical_repeat(Session, seed=2, now=now)

            async with Session() as session:
                reserved = await CanonicalRepeatPlanReservationService(
                    session
                ).reserve_next(source_id, after=now)
                assert reserved.outcome == "reserved"

            task_id = await _relink_legacy_transport(Session, source_id)

            async with Session() as session:
                verification = await CanonicalRepeatReservationVerifier(session).verify(
                    source_id
                )
                assert verification.outcome == "conflict"

            async with Session() as session:
                materialized = await CanonicalRepeatTransportAdapter(session).materialize(
                    source_id
                )
                assert materialized.outcome == "conflict"

            assert await _successor_count(Session, source_id) == 0
            async with Session() as session:
                source = await session.get(Publication, source_id)
                task = await session.get(PostTask, task_id)
                assert source is not None
                assert source.legacy_post_task_id == task_id
                assert task is not None and task.status == "pending"

            worker = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            replay = await worker.run_once(now=now + timedelta(minutes=20))
            assert replay.eligible == 0
            assert replay.materialized == 0
            assert await _successor_count(Session, source_id) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
