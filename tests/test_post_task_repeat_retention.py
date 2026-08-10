from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.post_task_retention import PostTaskRetentionService
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.scheduling import cleanup_runtime_fields, inherit_flags_for_repeat


async def _seed_repeat_handoff(
    Session,
    *,
    channel_id: int,
    now: datetime,
    with_successor: bool = True,
    pending_autodelete: bool = False,
) -> tuple[int, int, int | None, int | None]:
    async with Session() as session:
        root_when = now - timedelta(days=120)
        root_payload: dict[str, object] = {
            "type": "text",
            "text": f"Repeat retention {channel_id}",
            "repeat_on": True,
            "repeat_seconds": 3600,
            "result_ids": [channel_id * 100 + 1],
        }
        due = now + timedelta(hours=1)
        if pending_autodelete:
            root_payload.update(
                {
                    "autodelete_seconds": 3600,
                    "autodelete_effective_seconds": 3600,
                    "autodelete_at": due.isoformat(),
                    "autodeleted": False,
                }
            )
        root = PostTask(
            channel_id=channel_id,
            status="done",
            scheduled_at=root_when,
            payload=root_payload,
        )
        session.add(root)
        await session.commit()
        await session.refresh(root)
        root_id = int(root.id)

        root_publication = await mirror_legacy_post_task(session, root)
        assert root_publication is not None
        root_publication_id = int(root_publication.id)
        root_attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == root_publication_id,
                    PublicationAttempt.attempt == 1,
                )
            )
        ).scalar_one()
        root_attempt.finished_at = now - timedelta(days=120)
        if pending_autodelete:
            root_publication.meta = {
                **dict(root_publication.meta or {}),
                "runtime_options": {"autodelete_seconds": 3600},
                AUTODELETE_RUNTIME_META_KEY: {
                    "effective_seconds": 3600,
                    "scheduled_at": due.isoformat(),
                    "deleted": False,
                },
            }
        await session.commit()

        if not with_successor:
            return root_id, root_publication_id, None, None

        child_payload = cleanup_runtime_fields(dict(root.payload or {}))
        child_payload = inherit_flags_for_repeat(child_payload, root_id)
        child = PostTask(
            channel_id=channel_id,
            status="pending",
            scheduled_at=root_when + timedelta(hours=1),
            payload=child_payload,
        )
        session.add(child)
        await session.commit()
        await session.refresh(child)
        child_publication = await mirror_legacy_post_task(session, child)
        assert child_publication is not None
        return root_id, root_publication_id, int(child.id), int(child_publication.id)


def test_repeat_retention_remains_disabled_by_default(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-retention-default.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            root_id, _, child_id, _ = await _seed_repeat_handoff(
                Session,
                channel_id=951,
                now=now,
            )

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.deleted == 0
            assert tick.skipped_repeat == 1
            async with Session() as session:
                assert await session.get(PostTask, root_id) is not None
                assert child_id is not None
                assert await session.get(PostTask, child_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_terminal_repeat_retirement_requires_and_preserves_mirrored_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-retention-successor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            root_id, root_publication_id, child_id, child_publication_id = (
                await _seed_repeat_handoff(
                    Session,
                    channel_id=952,
                    now=now,
                )
            )
            assert child_id is not None and child_publication_id is not None

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_repeat_occurrences=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.eligible == 1
            assert tick.deleted == 1
            assert tick.skipped_repeat == 0
            assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, root_id) is None
                child = await session.get(PostTask, child_id)
                root_publication = await session.get(Publication, root_publication_id)
                child_publication = await session.get(Publication, child_publication_id)
                assert child is not None
                assert root_publication is not None and child_publication is not None
                assert root_publication.legacy_post_task_id is None
                assert child_publication.legacy_post_task_id == child_id
                assert (
                    child_publication.content_item_id == root_publication.content_item_id
                )
                retention = root_publication.meta["legacy_transport_retention"]
                assert retention["retired"] is True
                assert retention["terminal_status"] == "done"
                assert retention["repeat_group_id"] == root_id
                assert retention["repeat_successor_task_id"] == child_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_terminal_repeat_retirement_fails_closed_without_mirrored_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-retention-no-successor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            root_id, _, _, _ = await _seed_repeat_handoff(
                Session,
                channel_id=953,
                now=now,
                with_successor=False,
            )

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_repeat_occurrences=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.deleted == 0
            assert tick.skipped_repeat == 1
            async with Session() as session:
                assert await session.get(PostTask, root_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_retirement_never_hands_pending_autodelete_to_nonrepeat_worker(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-retention-pending-autodelete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            root_id, root_publication_id, child_id, _ = await _seed_repeat_handoff(
                Session,
                channel_id=954,
                now=now,
                pending_autodelete=True,
            )
            assert child_id is not None

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_pending_autodelete=True,
                    retire_successful_repeat_occurrences=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.deleted == 0
            assert tick.skipped_repeat == 0
            assert tick.skipped_pending_autodelete == 1
            async with Session() as session:
                root = await session.get(PostTask, root_id)
                publication = await session.get(Publication, root_publication_id)
                assert root is not None and publication is not None
                assert publication.legacy_post_task_id == root_id
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_retirement_fails_closed_on_canonical_group_conflict(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-retention-group-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            root_id, root_publication_id, child_id, _ = await _seed_repeat_handoff(
                Session,
                channel_id=955,
                now=now,
            )
            assert child_id is not None

            async with Session() as session:
                publication = await session.get(Publication, root_publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "repeat_group_id": root_id + 1000,
                }
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_repeat_occurrences=True,
                ).run_once(now=now)

            assert tick.deleted == 0
            assert tick.skipped_repeat == 1
            async with Session() as session:
                assert await session.get(PostTask, root_id) is not None
                assert await session.get(PostTask, child_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
