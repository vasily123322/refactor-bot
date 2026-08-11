from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_boot_recovery_reservation import (
    CanonicalRepeatBootRecoveryReservationService,
)
from app.services.canonical_repeat_boot_recovery_verifier import (
    CanonicalRepeatBootRecoveryVerifier,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import cleanup_runtime_fields
from app.workers.publication_scheduler import Scheduler as PublicationScheduler


_RUNTIME_OPTIONS = {
    "autodelete_views": 100,
    "autodelete_report": True,
    "nested": {"mode": "stable"},
}


async def _seed_legacy_group(
    Session,
    *,
    seed: int,
    source_at: datetime,
) -> tuple[tuple[int, int], tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=112000 + seed,
            username=f"repeat-boot-verify-{seed}",
            full_name=f"Repeat Boot Verify {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(112100 + seed),
            title=f"Repeat Boot Verify {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Boot verify"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        root_publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=source_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        root_task_id = int(root_publication.legacy_post_task_id or 0)
        root_task = await session.get(PostTask, root_task_id)
        assert root_task is not None

        second_payload = cleanup_runtime_fields(dict(root_task.payload or {}))
        second_payload["repeat_on"] = True
        second_payload["repeat_seconds"] = 3600
        second_payload["repeat_group_id"] = root_task_id
        second_task = PostTask(
            channel_id=int(root_task.channel_id),
            status="pending",
            payload=second_payload,
            dedupe_key=None,
            scheduled_at=source_at + timedelta(hours=1),
        )
        session.add(second_task)
        await session.commit()
        await session.refresh(second_task)
        second_publication = await mirror_legacy_post_task(session, second_task)
        assert second_publication is not None

        return (
            (int(root_publication.id), int(second_publication.id)),
            (root_task_id, int(second_task.id)),
        )


async def _reserve(
    session,
    *,
    publication_ids: tuple[int, int],
    after: datetime,
) -> None:
    result = await CanonicalRepeatBootRecoveryReservationService(session).reserve_group(
        publication_ids,
        after=after,
    )
    assert result.outcome == "reserved"


async def _legacy_boot_cleanup(
    session,
    *,
    task_ids: tuple[int, int],
    after: datetime,
) -> None:
    tasks = [await session.get(PostTask, task_id) for task_id in task_ids]
    assert all(task is not None for task in tasks)
    scheduler = PublicationScheduler(session, object())
    scheduler._boot_time = after  # noqa: SLF001 - exact production boot boundary
    remaining = await scheduler._boot_cleanup_repeats(  # noqa: SLF001
        session,
        [task for task in tasks if task is not None],
    )
    assert remaining == []


def test_group_verifier_tracks_real_legacy_boot_cleanup_and_survives_source_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-verifier.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_ids, task_ids = await _seed_legacy_group(
                Session,
                seed=1,
                source_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_ids=publication_ids, after=after)
                verifier = CanonicalRepeatBootRecoveryVerifier(session)
                pending = await verifier.verify(publication_ids)
                assert pending.outcome == "pending"

                await _legacy_boot_cleanup(session, task_ids=task_ids, after=after)
                matched = await verifier.verify(publication_ids)
                assert matched.outcome == "matched"
                assert matched.successor_publication_id is not None
                assert matched.successor_schedule_entry_id is not None
                assert matched.successor_legacy_post_task_id is not None

                for publication_id, task_id in zip(publication_ids, task_ids, strict=True):
                    publication = await session.get(Publication, publication_id)
                    task = await session.get(PostTask, task_id)
                    assert publication is not None and task is not None
                    assert publication.status == "skipped"
                    publication.legacy_post_task_id = None
                    await session.delete(task)
                await session.commit()

                after_retirement = await verifier.verify(publication_ids)
                assert after_retirement.outcome == "matched"
                assert (
                    after_retirement.successor_publication_id
                    == matched.successor_publication_id
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_group_verifier_fails_closed_on_successor_runtime_drift(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-verifier-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_ids, task_ids = await _seed_legacy_group(
                Session,
                seed=2,
                source_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_ids=publication_ids, after=after)
                await _legacy_boot_cleanup(session, task_ids=task_ids, after=after)
                verifier = CanonicalRepeatBootRecoveryVerifier(session)
                matched = await verifier.verify(publication_ids)
                assert matched.outcome == "matched"
                assert matched.successor_schedule_entry_id is not None

                child_schedule = await session.get(
                    ScheduleEntry,
                    int(matched.successor_schedule_entry_id),
                )
                assert child_schedule is not None
                child_schedule.meta = {
                    **dict(child_schedule.meta or {}),
                    "runtime_options": {"autodelete_views": 999},
                }
                await session.commit()

                conflict = await verifier.verify(publication_ids)
                assert conflict.outcome == "conflict"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_group_verifier_rejects_mixed_source_lifecycle_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-verifier-mixed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_ids, task_ids = await _seed_legacy_group(
                Session,
                seed=3,
                source_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_ids=publication_ids, after=after)
                first_task = await session.get(PostTask, task_ids[0])
                assert first_task is not None
                first_task.status = "skipped"
                await session.commit()
                await LegacyPublicationBridge(session).reconcile_task(first_task)

                conflict = await CanonicalRepeatBootRecoveryVerifier(session).verify(
                    publication_ids
                )
                assert conflict.outcome == "conflict"
        finally:
            await engine.dispose()

    asyncio.run(run())
