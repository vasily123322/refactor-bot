from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_boot_recovery_planner import (
    CanonicalRepeatBootRecoveryPlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import compute_next_repeat_time


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
            tg_user_id=110000 + seed,
            username=f"repeat-boot-group-{seed}",
            full_name=f"Repeat Boot Group {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(110100 + seed),
            title=f"Repeat Boot Group {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Boot group"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        root = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=source_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        root_task_id = int(root.legacy_post_task_id or 0)
        root_schedule = await session.get(ScheduleEntry, int(root.schedule_entry_id or 0))
        assert root_schedule is not None

        canonical_meta = {
            "repeat_group_id": root_task_id,
            "runtime_options": deepcopy(_RUNTIME_OPTIONS),
        }
        second_schedule = ScheduleEntry(
            content_item_id=int(root.content_item_id),
            content_revision=int(root.content_revision),
            channel_id=int(root.channel_id),
            scheduled_at=source_at + timedelta(hours=1),
            timezone=root_schedule.timezone,
            status="pending",
            repeat_rule={"enabled": True, "seconds": 3600},
            meta=deepcopy(canonical_meta),
        )
        session.add(second_schedule)
        await session.flush()
        second = Publication(
            schedule_entry_id=int(second_schedule.id),
            content_item_id=int(root.content_item_id),
            content_revision=int(root.content_revision),
            channel_id=int(root.channel_id),
            status="queued",
            meta=deepcopy(canonical_meta),
        )
        session.add(second)
        await session.commit()
        await session.refresh(second)
        return int(root.id), int(second.id), root_task_id


async def _remove_root_transport(session, *, publication_id: int, task_id: int) -> None:
    publication = await session.get(Publication, publication_id)
    task = await session.get(PostTask, task_id)
    assert publication is not None and task is not None
    publication.legacy_post_task_id = None
    await session.delete(task)
    await session.commit()


async def _add_existing_successor(
    session,
    *,
    source_publication_id: int,
    repeat_group_id: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    source = await session.get(Publication, source_publication_id)
    assert source is not None
    source_schedule = await session.get(
        ScheduleEntry,
        int(source.schedule_entry_id or 0),
    )
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
    return int(publication.id), int(schedule.id)


def test_boot_group_plan_uses_only_canonical_sources_after_transport_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-group-no-transport.db'}"
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
                await _remove_root_transport(
                    session,
                    publication_id=root_id,
                    task_id=task_id,
                )
                plan = await CanonicalRepeatBootRecoveryPlanner(session).plan_group(
                    [root_id, second_id],
                    after=after,
                )

                assert plan is not None
                assert plan.anchor_publication_id == root_id
                assert plan.repeat_group_id == task_id
                assert plan.repeat_seconds == 3600
                assert plan.scheduled_at == source_at + timedelta(hours=4)
                assert plan.runtime_options == _RUNTIME_OPTIONS
                assert [source.publication_id for source in plan.sources] == [
                    root_id,
                    second_id,
                ]
                assert [source.scheduled_at for source in plan.sources] == [
                    source_at,
                    source_at + timedelta(hours=1),
                ]
                assert plan.existing_publication_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_boot_group_plan_matches_legacy_anchor_math_and_rejects_reordered_sources(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-group-order.db'}"
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
                planner = CanonicalRepeatBootRecoveryPlanner(session)
                plan = await planner.plan_group([root_id, second_id], after=after)
                assert plan is not None
                assert plan.scheduled_at == compute_next_repeat_time(
                    source_at,
                    3600,
                    after,
                )
                assert await planner.plan_group(
                    [second_id, root_id],
                    after=after,
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_boot_group_plan_fails_closed_on_runtime_interval_or_content_drift(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-group-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            root_id, second_id, _ = await _seed_group(
                Session,
                seed=3,
                source_at=source_at,
            )

            async with Session() as session:
                planner = CanonicalRepeatBootRecoveryPlanner(session)
                second = await session.get(Publication, second_id)
                assert second is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(second.schedule_entry_id or 0),
                )
                assert schedule is not None
                original_meta = deepcopy(schedule.meta)
                original_rule = deepcopy(schedule.repeat_rule)

                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": {"autodelete_views": 999},
                }
                await session.commit()
                assert await planner.plan_group([root_id, second_id], after=after) is None

                schedule.meta = deepcopy(original_meta)
                schedule.repeat_rule = {"enabled": True, "seconds": 1800}
                await session.commit()
                assert await planner.plan_group([root_id, second_id], after=after) is None

                schedule.repeat_rule = deepcopy(original_rule)
                root = await session.get(Publication, root_id)
                assert root is not None
                revision = (
                    await session.execute(
                        ContentRevision.__table__.select().where(
                            ContentRevision.content_item_id == int(root.content_item_id),
                            ContentRevision.revision == int(root.content_revision),
                        )
                    )
                ).mappings().one()
                session.add(
                    ContentRevision(
                        content_item_id=int(root.content_item_id),
                        revision=2,
                        document=deepcopy(revision["document"]),
                        source="editor",
                        created_by_tg_user_id=revision["created_by_tg_user_id"],
                        meta={},
                    )
                )
                second.content_revision = 2
                schedule.content_revision = 2
                await session.commit()
                assert await planner.plan_group([root_id, second_id], after=after) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_boot_group_plan_detects_one_existing_exact_canonical_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-group-existing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            root_id, second_id, task_id = await _seed_group(
                Session,
                seed=4,
                source_at=source_at,
            )

            async with Session() as session:
                successor_id, successor_schedule_id = await _add_existing_successor(
                    session,
                    source_publication_id=root_id,
                    repeat_group_id=task_id,
                    scheduled_at=target_at,
                )
                plan = await CanonicalRepeatBootRecoveryPlanner(session).plan_group(
                    [root_id, second_id],
                    after=after,
                )
                assert plan is not None
                assert plan.scheduled_at == target_at
                assert plan.existing_publication_id == successor_id
                assert plan.existing_schedule_entry_id == successor_schedule_id
        finally:
            await engine.dispose()

    asyncio.run(run())
