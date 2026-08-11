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
from app.services.canonical_repeat_recovery_reservation import (
    CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY,
    CanonicalRepeatRecoveryReservationService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_queued_repeat(Session, *, seed: int, scheduled_at: datetime) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=104000 + seed,
            username=f"repeat-recovery-reservation-{seed}",
            full_name=f"Repeat Recovery Reservation {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(104100 + seed),
            title=f"Repeat Recovery Reservation {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Recovery reserve"}]
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
        return int(publication.id), int(publication.legacy_post_task_id or 0)


async def _counts(session) -> tuple[int, int, int]:
    publications = int((await session.execute(select(func.count(Publication.id)))).scalar_one())
    schedules = int((await session.execute(select(func.count(ScheduleEntry.id)))).scalar_one())
    tasks = int((await session.execute(select(func.count(PostTask.id)))).scalar_one())
    return publications, schedules, tasks


def test_recovery_reservation_is_source_only_idempotent_and_transport_independent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-reservation.db'}"
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
                before = await _counts(session)

                service = CanonicalRepeatRecoveryReservationService(session)
                first = await service.reserve_recovery(
                    publication_id,
                    after=source_at + timedelta(hours=3, minutes=30),
                )
                after_first = await _counts(session)
                second = await service.reserve_recovery(
                    publication_id,
                    after=source_at + timedelta(hours=3, minutes=30),
                )
                after_second = await _counts(session)

                assert first.outcome == "reserved"
                assert second.outcome == "already_reserved"
                assert before == after_first == after_second

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert schedule is not None
                snapshot = publication.meta[
                    CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY
                ]
                assert snapshot == schedule.meta[
                    CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY
                ]
                assert snapshot["source_scheduled_at"] == source_at.isoformat()
                assert snapshot["scheduled_at"] == (
                    source_at + timedelta(hours=4)
                ).isoformat()
                assert snapshot["repeat_group_id"] == task_id
                assert snapshot["runtime_options"] == {
                    "autodelete_views": 100,
                    "autodelete_report": True,
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_reservation_never_overwrites_when_source_slot_changes(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-reservation-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed_queued_repeat(
                Session, seed=2, scheduled_at=source_at
            )

            async with Session() as session:
                service = CanonicalRepeatRecoveryReservationService(session)
                first = await service.reserve_recovery(
                    publication_id,
                    after=source_at + timedelta(hours=3, minutes=30),
                )
                assert first.outcome == "reserved"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert schedule is not None
                original = dict(
                    publication.meta[CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY]
                )

                schedule.scheduled_at = source_at + timedelta(minutes=30)
                await session.commit()
                conflict = await service.reserve_recovery(
                    publication_id,
                    after=source_at + timedelta(hours=3, minutes=30),
                )
                assert conflict.outcome == "conflict"

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert schedule is not None
                assert publication.meta[
                    CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY
                ] == original
                assert schedule.meta[
                    CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY
                ] == original
        finally:
            await engine.dispose()

    asyncio.run(run())
