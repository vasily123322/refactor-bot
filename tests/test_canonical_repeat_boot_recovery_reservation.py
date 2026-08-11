from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_boot_recovery_reservation import (
    CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY,
    CanonicalRepeatBootRecoveryReservationService,
)
from app.services.publication_bridge import LegacyPublicationBridge


_RUNTIME_OPTIONS = {
    "autodelete_views": 100,
    "autodelete_report": True,
    "nested": {"mode": "stable"},
}


async def _seed_group(
    Session,
    *,
    seed: int,
    source_at: datetime,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=111000 + seed,
            username=f"repeat-boot-reservation-{seed}",
            full_name=f"Repeat Boot Reservation {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(111100 + seed),
            title=f"Repeat Boot Reservation {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Boot reservation"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        root = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=source_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        task_id = int(root.legacy_post_task_id or 0)
        root_schedule = await session.get(ScheduleEntry, int(root.schedule_entry_id or 0))
        assert root_schedule is not None
        meta = {
            "repeat_group_id": task_id,
            "runtime_options": deepcopy(_RUNTIME_OPTIONS),
        }
        schedule = ScheduleEntry(
            content_item_id=int(root.content_item_id),
            content_revision=int(root.content_revision),
            channel_id=int(root.channel_id),
            scheduled_at=source_at + timedelta(hours=1),
            timezone=root_schedule.timezone,
            status="pending",
            repeat_rule={"enabled": True, "seconds": 3600},
            meta=deepcopy(meta),
        )
        session.add(schedule)
        await session.flush()
        second = Publication(
            schedule_entry_id=int(schedule.id),
            content_item_id=int(root.content_item_id),
            content_revision=int(root.content_revision),
            channel_id=int(root.channel_id),
            status="queued",
            meta=deepcopy(meta),
        )
        session.add(second)
        await session.commit()
        await session.refresh(second)
        return int(root.id), int(second.id), task_id


async def _counts(session) -> tuple[int, int, int]:
    publications = int((await session.execute(select(func.count(Publication.id)))).scalar_one())
    schedules = int((await session.execute(select(func.count(ScheduleEntry.id)))).scalar_one())
    tasks = int((await session.execute(select(func.count(PostTask.id)))).scalar_one())
    return publications, schedules, tasks


async def _add_successor(
    session,
    *,
    source_publication_id: int,
    repeat_group_id: int,
    scheduled_at: datetime,
) -> int:
    source = await session.get(Publication, source_publication_id)
    assert source is not None
    source_schedule = await session.get(ScheduleEntry, int(source.schedule_entry_id or 0))
    assert source_schedule is not None
    meta = {
        "repeat_group_id": repeat_group_id,
        "runtime_options": deepcopy(_RUNTIME_OPTIONS),
    }
    schedule = ScheduleEntry(
        content_item_id=int(source.content_item_id),
        content_revision=int(source.content_revision),
        channel_id=int(source.channel_id),
        scheduled_at=scheduled_at,
        timezone=source_schedule.timezone,
        status="pending",
        repeat_rule={"enabled": True, "seconds": 3600},
        meta=deepcopy(meta),
    )
    session.add(schedule)
    await session.flush()
    publication = Publication(
        schedule_entry_id=int(schedule.id),
        content_item_id=int(source.content_item_id),
        content_revision=int(source.content_revision),
        channel_id=int(source.channel_id),
        status="queued",
        meta=deepcopy(meta),
    )
    session.add(publication)
    await session.commit()
    await session.refresh(publication)
    return int(publication.id)


def test_group_reservation_is_transport_independent_atomic_and_idempotent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-reservation.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            root_id, second_id, task_id = await _seed_group(
                Session,
                seed=1,
                source_at=source_at,
            )

            async with Session() as session:
                root = await session.get(Publication, root_id)
                task = await session.get(PostTask, task_id)
                assert root is not None and task is not None
                root.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()
                before = await _counts(session)

                service = CanonicalRepeatBootRecoveryReservationService(session)
                reserved = await service.reserve_group(
                    [root_id, second_id],
                    after=after,
                )
                after_reserved = await _counts(session)
                repeated = await service.reserve_group(
                    [root_id, second_id],
                    after=after,
                )
                after_repeated = await _counts(session)

                assert reserved.outcome == "reserved"
                assert repeated.outcome == "already_reserved"
                assert before == after_reserved == after_repeated

                snapshots = []
                for publication_id in (root_id, second_id):
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    schedule = await session.get(
                        ScheduleEntry,
                        int(publication.schedule_entry_id or 0),
                    )
                    assert schedule is not None
                    publication_snapshot = dict(publication.meta or {}).get(
                        CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY
                    )
                    schedule_snapshot = dict(schedule.meta or {}).get(
                        CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY
                    )
                    assert isinstance(publication_snapshot, dict)
                    assert publication_snapshot == schedule_snapshot
                    snapshots.append(publication_snapshot)

                assert snapshots[0] == snapshots[1]
                assert snapshots[0]["anchor_publication_id"] == root_id
                assert snapshots[0]["repeat_group_id"] == task_id
                assert snapshots[0]["scheduled_at"] == (
                    source_at + timedelta(hours=4)
                ).isoformat()
                assert [row["publication_id"] for row in snapshots[0]["sources"]] == [
                    root_id,
                    second_id,
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_group_reservation_refuses_one_sided_metadata_without_repair(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-reservation-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            root_id, second_id, _ = await _seed_group(
                Session,
                seed=2,
                source_at=source_at,
            )

            async with Session() as session:
                root = await session.get(Publication, root_id)
                assert root is not None
                root.meta = {
                    **dict(root.meta or {}),
                    CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY: {
                        "version": 999
                    },
                }
                await session.commit()

                result = await CanonicalRepeatBootRecoveryReservationService(
                    session
                ).reserve_group([root_id, second_id], after=after)
                assert result.outcome == "conflict"

                root = await session.get(Publication, root_id)
                second = await session.get(Publication, second_id)
                assert root is not None and second is not None
                assert dict(root.meta or {})[
                    CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY
                ] == {"version": 999}
                assert CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY not in dict(
                    second.meta or {}
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_successor_prevents_group_reservation_write(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-reservation-existing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            root_id, second_id, task_id = await _seed_group(
                Session,
                seed=3,
                source_at=source_at,
            )

            async with Session() as session:
                successor_id = await _add_successor(
                    session,
                    source_publication_id=root_id,
                    repeat_group_id=task_id,
                    scheduled_at=source_at + timedelta(hours=4),
                )
                before = await _counts(session)
                result = await CanonicalRepeatBootRecoveryReservationService(
                    session
                ).reserve_group([root_id, second_id], after=after)
                after_counts = await _counts(session)

                assert result.outcome == "existing_successor"
                assert result.plan is not None
                assert result.plan.existing_publication_id == successor_id
                assert before == after_counts
                for publication_id in (root_id, second_id):
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    schedule = await session.get(
                        ScheduleEntry,
                        int(publication.schedule_entry_id or 0),
                    )
                    assert schedule is not None
                    assert CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY not in dict(
                        publication.meta or {}
                    )
                    assert CANONICAL_REPEAT_BOOT_RECOVERY_RESERVATION_META_KEY not in dict(
                        schedule.meta or {}
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())
