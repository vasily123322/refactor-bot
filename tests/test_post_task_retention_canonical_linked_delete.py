from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.content_plan_history_identity import (
    LEGACY_POST_TASK_CALLBACK_ID_META_KEY,
    HistoryPublicationIdentityKind,
    resolve_history_publication_identity,
)
from app.services.post_task_retention import PostTaskRetentionService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    INTENTIONAL_LEGACY_EXECUTION_MODE,
)


async def _seed_successful_linked(
    Session,
    *,
    channel_id: int,
    now: datetime,
    execution_mode: str,
    repeat: bool = False,
) -> tuple[int, int, int, int]:
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Canonical retention {channel_id}",
                    }
                ]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            repeat_rule={"enabled": True, "seconds": 3600} if repeat else None,
        )
        task_id = int(publication.legacy_post_task_id or 0)
        schedule_id = int(publication.schedule_entry_id or 0)
        task = await session.get(PostTask, task_id)
        schedule = await session.get(ScheduleEntry, schedule_id)
        assert task is not None and schedule is not None

        message_id = 9_000_000 + channel_id
        root_at = now - timedelta(days=121)
        task.status = "done"
        task.scheduled_at = root_at
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [message_id],
            **(
                {
                    "repeat_on": True,
                    "repeat_seconds": 3600,
                    "repeat_group_id": task_id,
                }
                if repeat
                else {}
            ),
        }
        publication.execution_mode = execution_mode
        publication.status = "published"
        publication.telegram_message_ids = [message_id]
        publication.result_link = None
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": {},
            **({"repeat_group_id": task_id} if repeat else {}),
        }
        schedule.status = "completed"
        schedule.scheduled_at = root_at
        if repeat:
            schedule.repeat_rule = {"enabled": True, "seconds": 3600}
            schedule.meta = {
                **dict(schedule.meta or {}),
                "repeat_group_id": task_id,
            }
        await session.commit()

        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        publication.execution_mode = execution_mode
        publication.status = "published"
        publication.telegram_message_ids = [message_id]
        publication.result_link = None
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": {},
            **({"repeat_group_id": task_id} if repeat else {}),
        }
        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == int(publication.id),
                    PublicationAttempt.attempt == int(publication.attempt_count),
                )
            )
        ).scalar_one()
        attempt.status = "published"
        attempt.telegram_message_ids = [message_id]
        attempt.finished_at = now - timedelta(days=120)
        schedule = await session.get(ScheduleEntry, schedule_id)
        assert schedule is not None
        schedule.status = "completed"
        schedule.scheduled_at = root_at
        if repeat:
            schedule.repeat_rule = {"enabled": True, "seconds": 3600}
            schedule.meta = {
                **dict(schedule.meta or {}),
                "repeat_group_id": task_id,
            }
        await session.commit()
        return task_id, int(publication.id), int(attempt.id), schedule_id


async def _add_posttask_free_repeat_successor(
    Session,
    *,
    root_publication_id: int,
    root_task_id: int,
    root_schedule_id: int,
    wrong_group: bool = False,
) -> int:
    async with Session() as session:
        root = await session.get(Publication, root_publication_id)
        root_schedule = await session.get(ScheduleEntry, root_schedule_id)
        assert root is not None and root_schedule is not None
        group_id = root_task_id + (1 if wrong_group else 0)
        meta = {
            "runtime_options": {},
            "repeat_group_id": group_id,
            "canonical_repeat_source_publication_id": root_publication_id,
            "canonical_repeat_transport_adapter": True,
            "canonical_repeat_posttask_free": True,
        }
        successor_schedule = ScheduleEntry(
            content_item_id=int(root.content_item_id),
            content_revision=int(root.content_revision),
            channel_id=int(root.channel_id),
            scheduled_at=root_schedule.scheduled_at + timedelta(hours=1),
            timezone=root_schedule.timezone,
            status="pending",
            repeat_rule={"enabled": True, "seconds": 3600},
            meta=dict(meta),
        )
        successor = Publication(
            schedule_entry_id=None,
            content_item_id=int(root.content_item_id),
            content_revision=int(root.content_revision),
            channel_id=int(root.channel_id),
            status="queued",
            execution_mode=CANONICAL_EXECUTION_MODE,
            repeat_source_publication_id=root_publication_id,
            meta=dict(meta),
        )
        session.add_all([successor_schedule, successor])
        await session.flush()
        successor.schedule_entry_id = int(successor_schedule.id)
        await session.commit()
        return int(successor.id)


def test_successful_canonical_linked_transport_is_deleted_and_callback_alias_survives(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-linked-delete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id, _, _ = await _seed_successful_linked(
                Session,
                channel_id=941,
                now=now,
                execution_mode=CANONICAL_EXECUTION_MODE,
            )

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)
                assert tick.selected == 1
                assert tick.eligible == 1
                assert tick.deleted == 1
                assert tick.skipped_content_linkage == 0
                assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.execution_mode == CANONICAL_EXECUTION_MODE
                assert publication.legacy_post_task_id is None
                meta = dict(publication.meta or {})
                assert meta[LEGACY_POST_TASK_CALLBACK_ID_META_KEY] == task_id
                assert meta["legacy_transport_retention"]["retired"] is True
                identity = await resolve_history_publication_identity(
                    session,
                    legacy_post_task_id=task_id,
                )
                assert identity.kind is HistoryPublicationIdentityKind.CANONICAL_LINKED
                assert identity.publication_id == publication_id
                assert identity.channel_id == int(publication.channel_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_successful_intentional_legacy_transport_remains_posttask_backed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'intentional-legacy-retained.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id, _, _ = await _seed_successful_linked(
                Session,
                channel_id=942,
                now=now,
                execution_mode=INTENTIONAL_LEGACY_EXECUTION_MODE,
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
                assert tick.skipped_content_linkage == 1
                assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
                assert LEGACY_POST_TASK_CALLBACK_ID_META_KEY not in dict(
                    publication.meta or {}
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unsuccessful_canonical_linked_transport_remains_guarded(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-unsuccessful-retained.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=943,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "failed"}]
                    ),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id)
                )
                task_id = int(publication.legacy_post_task_id or 0)
                publication_id = int(publication.id)
                task = await session.get(PostTask, task_id)
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert task is not None and schedule is not None
                task.status = "failed"
                publication.execution_mode = CANONICAL_EXECUTION_MODE
                await session.commit()
                publication = await LegacyPublicationBridge(session).reconcile(publication_id)
                publication.execution_mode = CANONICAL_EXECUTION_MODE
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == int(publication.attempt_count),
                        )
                    )
                ).scalar_one()
                attempt.finished_at = now - timedelta(days=120)
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                ).run_once(now=now)
                assert tick.selected == 1
                assert tick.deleted == 0
                assert tick.skipped_content_linkage == 1
                assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_duplicate_callback_identity_blocks_successful_canonical_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-alias-ambiguous.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id, _, schedule_id = await _seed_successful_linked(
                Session,
                channel_id=944,
                now=now,
                execution_mode=CANONICAL_EXECUTION_MODE,
            )

            async with Session() as session:
                root = await session.get(Publication, publication_id)
                root_schedule = await session.get(ScheduleEntry, schedule_id)
                assert root is not None and root_schedule is not None
                alias_schedule = ScheduleEntry(
                    content_item_id=int(root.content_item_id),
                    content_revision=int(root.content_revision),
                    channel_id=int(root.channel_id),
                    scheduled_at=root_schedule.scheduled_at + timedelta(hours=2),
                    timezone=root_schedule.timezone,
                    status="pending",
                    meta={},
                )
                alias_publication = Publication(
                    schedule_entry_id=None,
                    content_item_id=int(root.content_item_id),
                    content_revision=int(root.content_revision),
                    channel_id=int(root.channel_id),
                    status="queued",
                    execution_mode=CANONICAL_EXECUTION_MODE,
                    meta={LEGACY_POST_TASK_CALLBACK_ID_META_KEY: task_id},
                )
                session.add_all([alias_schedule, alias_publication])
                await session.flush()
                alias_publication.schedule_entry_id = int(alias_schedule.id)
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)
                assert tick.selected == 1
                assert tick.deleted == 0
                assert tick.skipped_content_linkage == 1
                assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                root = await session.get(Publication, publication_id)
                assert root is not None and root.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_successful_repeat_root_deletes_after_posttask_free_handoff(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-delete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id, _, schedule_id = await _seed_successful_linked(
                Session,
                channel_id=945,
                now=now,
                execution_mode=CANONICAL_EXECUTION_MODE,
                repeat=True,
            )
            successor_id = await _add_posttask_free_repeat_successor(
                Session,
                root_publication_id=publication_id,
                root_task_id=task_id,
                root_schedule_id=schedule_id,
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
                assert tick.eligible == 1
                assert tick.deleted == 1
                assert tick.skipped_repeat == 0
                assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                root = await session.get(Publication, publication_id)
                successor = await session.get(Publication, successor_id)
                assert root is not None and successor is not None
                assert root.legacy_post_task_id is None
                meta = dict(root.meta or {})
                assert meta[LEGACY_POST_TASK_CALLBACK_ID_META_KEY] == task_id
                assert meta["legacy_transport_retention"][
                    "repeat_successor_publication_id"
                ] == successor_id
                assert successor.legacy_post_task_id is None
                assert successor.repeat_source_publication_id == publication_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_wrong_repeat_group_still_blocks_canonical_root_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-wrong-group.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id, _, schedule_id = await _seed_successful_linked(
                Session,
                channel_id=946,
                now=now,
                execution_mode=CANONICAL_EXECUTION_MODE,
                repeat=True,
            )
            await _add_posttask_free_repeat_successor(
                Session,
                root_publication_id=publication_id,
                root_task_id=task_id,
                root_schedule_id=schedule_id,
                wrong_group=True,
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
                assert tick.failures == 0

            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                root = await session.get(Publication, publication_id)
                assert root is not None and root.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())
