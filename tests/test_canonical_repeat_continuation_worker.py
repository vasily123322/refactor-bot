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
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import as_utc
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed_terminal_repeat(
    Session,
    *,
    seed: int,
    source_at: datetime,
    repeat_seconds: int = 60,
    canonical_delivery: bool = True,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=204000 + seed,
            username=f"repeat-continuation-{seed}",
            full_name=f"Repeat Continuation {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100204000 + seed),
            title=f"Repeat Continuation {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Repeat continuation {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=source_at,
            repeat_rule={"enabled": True, "seconds": repeat_seconds},
            runtime_options={"silent": True},
        )
        assert publication.legacy_post_task_id is not None
        task = await session.get(PostTask, int(publication.legacy_post_task_id))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None

        # Reconstruct the durable shape produced by successful canonical delivery after
        # atomic legacy transport retirement. Keep repeat_group_id lineage but remove the
        # compatibility marker together with the physical PostTask.
        task_id = int(task.id)
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [6100 + seed]
        publication.result_link = None
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[6100 + seed],
                error=None,
                meta={"canonical_delivery": canonical_delivery},
                finished_at=source_at + timedelta(seconds=5),
            )
        )
        await session.delete(task)
        await session.commit()
        assert await session.get(PostTask, task_id) is None
        return int(publication.id)


async def _successors(Session, source_publication_id: int):
    async with Session() as session:
        source = await session.get(Publication, source_publication_id)
        assert source is not None
        group_id = int(dict(source.meta or {})["repeat_group_id"])
        rows = (
            await session.execute(
                select(Publication, ScheduleEntry)
                .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
                .where(
                    Publication.id != source_publication_id,
                    Publication.content_item_id == int(source.content_item_id),
                    Publication.content_revision == int(source.content_revision),
                    Publication.channel_id == int(source.channel_id),
                    ScheduleEntry.meta["repeat_group_id"].as_integer() == group_id,
                )
                .order_by(Publication.id.asc())
            )
        ).all()
        return rows


def test_terminal_canonical_repeat_source_materializes_exactly_one_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-continuation.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_terminal_repeat(
                Session,
                seed=1,
                source_at=now - timedelta(minutes=10),
                repeat_seconds=60,
            )
            worker = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )

            first = await worker.run_once(now=now)
            assert first.eligible == 1
            assert first.reserved == 1
            assert first.materialized == 1
            assert first.conflicts == 0
            assert first.failures == 0

            rows = await _successors(Session, source_id)
            assert len(rows) == 1
            successor, schedule = rows[0]
            assert successor.status == "queued"
            assert successor.legacy_post_task_id is not None
            assert schedule.status == "pending"
            # First fixed-delay slot strictly after `now`: source 11:50 + 60s cadence
            # advances through missed slots to 12:01.
            assert as_utc(schedule.scheduled_at) == now + timedelta(minutes=1)
            assert dict(successor.meta or {})["canonical_repeat_source_publication_id"] == source_id
            task = await Session().get(PostTask, int(successor.legacy_post_task_id))
            if task is not None:
                await task.__class__  # pragma: no cover - never executed; keep type narrow

            async with Session() as session:
                task = await session.get(PostTask, int(successor.legacy_post_task_id))
                assert task is not None
                assert task.status == "pending"
                assert task.dedupe_key == (
                    f"canonical-repeat:{source_id}:{as_utc(schedule.scheduled_at).isoformat()}"
                )

            second = await worker.run_once(now=now + timedelta(seconds=1))
            assert second.already_complete == 1
            assert second.materialized == 0
            assert second.conflicts == 0
            assert len(await _successors(Session, source_id)) == 1

            async with Session() as session:
                verification = await CanonicalRepeatReservationVerifier(session).verify(source_id)
                assert verification.outcome == "matched"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pending_reservation_is_materialized_without_replanning_after_slot_passes(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-reservation-recovery.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            reserve_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_terminal_repeat(
                Session,
                seed=2,
                source_at=reserve_at - timedelta(minutes=2),
                repeat_seconds=60,
            )

            async with Session() as session:
                reservation = await CanonicalRepeatPlanReservationService(session).reserve_next(
                    source_id,
                    after=reserve_at,
                )
                assert reservation.outcome == "reserved"
                assert reservation.plan is not None
                reserved_at = as_utc(reservation.plan.scheduled_at)
                assert reserved_at == reserve_at + timedelta(minutes=1)

            # Simulate a process crash after reservation and a restart long after the
            # reserved slot. The worker must materialize the durable reservation rather
            # than replan to a new future slot.
            worker = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            recovered = await worker.run_once(now=reserve_at + timedelta(minutes=20))
            assert recovered.materialized == 1
            assert recovered.reserved == 0
            assert recovered.conflicts == 0

            rows = await _successors(Session, source_id)
            assert len(rows) == 1
            _, successor_schedule = rows[0]
            assert as_utc(successor_schedule.scheduled_at) == reserved_at

            async with Session() as session:
                source = await session.get(Publication, source_id)
                source_schedule = await session.get(
                    ScheduleEntry,
                    int(source.schedule_entry_id),
                )
                assert source is not None and source_schedule is not None
                assert (
                    dict(source.meta or {})[CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY]
                    == dict(source_schedule.meta or {})[
                        CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY
                    ]
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_origin_terminal_repeat_is_never_selected(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-legacy-origin.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_terminal_repeat(
                Session,
                seed=3,
                source_at=now - timedelta(minutes=2),
                canonical_delivery=False,
            )
            worker = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            tick = await worker.run_once(now=now)
            assert tick.eligible == 0
            assert tick.materialized == 0
            assert await _successors(Session, source_id) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_keyset_cursor_does_not_skip_eligible_rows_when_batch_fills(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-keyset.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_ids = []
            for seed in range(10, 14):
                source_ids.append(
                    await _seed_terminal_repeat(
                        Session,
                        seed=seed,
                        source_at=now - timedelta(minutes=2),
                    )
                )

            worker = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=2,
                scan_limit=20,
            )
            first = await worker.run_once(now=now)
            second = await worker.run_once(now=now)
            assert first.eligible == 2
            assert second.eligible == 2
            assert first.cursor_reset is False
            assert len([source_id for source_id in source_ids if (await _successors(Session, source_id))]) == 4
        finally:
            await engine.dispose()

    asyncio.run(run())
