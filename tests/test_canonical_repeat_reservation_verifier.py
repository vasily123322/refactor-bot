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
from app.services.canonical_repeat_plan_reservation import (
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
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
            tg_user_id=99700 + seed,
            username=f"repeat-verify-{seed}",
            full_name=f"Repeat Verify {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10099700 + seed),
            title=f"Repeat Verify {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Verify repeat"}]
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
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [99800 + seed],
            "result_link": f"https://t.me/c/{99700 + seed}/{99800 + seed}",
        }
        await session.commit()
        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        assert publication.status == "published"
        return int(publication.id), task_id


async def _reserve_and_schedule_successor(
    session,
    *,
    publication_id: int,
    task_id: int,
    source_at: datetime,
) -> int:
    reservation = await CanonicalRepeatPlanReservationService(session).reserve_next(
        publication_id,
        after=source_at + timedelta(minutes=10),
    )
    assert reservation.outcome == "reserved"

    pending = await CanonicalRepeatReservationVerifier(session).verify(publication_id)
    assert pending.outcome == "pending"

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

    matched = await CanonicalRepeatReservationVerifier(session).verify(publication_id)
    assert matched.outcome == "matched"
    assert matched.successor_publication_id is not None
    assert matched.successor_schedule_entry_id is not None
    assert matched.successor_legacy_post_task_id is not None
    return int(matched.successor_publication_id)


def test_verifier_matches_legacy_successor_and_survives_root_transport_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-reservation-verify.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime.now(timezone.utc) + timedelta(hours=2)
            publication_id, task_id = await _seed_published_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                successor_id = await _reserve_and_schedule_successor(
                    session,
                    publication_id=publication_id,
                    task_id=task_id,
                    source_at=source_at,
                )

                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                after_retirement = await CanonicalRepeatReservationVerifier(
                    session
                ).verify(publication_id)
                assert after_retirement.outcome == "matched"
                assert after_retirement.successor_publication_id == successor_id
                assert after_retirement.successor_legacy_post_task_id is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_verifier_fails_closed_when_successor_runtime_intent_drifted(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-reservation-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime.now(timezone.utc) + timedelta(hours=2)
            publication_id, task_id = await _seed_published_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                successor_id = await _reserve_and_schedule_successor(
                    session,
                    publication_id=publication_id,
                    task_id=task_id,
                    source_at=source_at,
                )
                successor = await session.get(Publication, successor_id)
                assert successor is not None
                successor_schedule = await session.get(
                    ScheduleEntry,
                    int(successor.schedule_entry_id or 0),
                )
                assert successor_schedule is not None
                successor_schedule.meta = {
                    **dict(successor_schedule.meta or {}),
                    "runtime_options": {"autodelete_views": 999},
                }
                await session.commit()

                conflict = await CanonicalRepeatReservationVerifier(session).verify(
                    publication_id
                )
                assert conflict.outcome == "conflict"
        finally:
            await engine.dispose()

    asyncio.run(run())
