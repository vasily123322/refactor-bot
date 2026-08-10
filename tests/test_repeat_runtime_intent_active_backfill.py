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
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.repeat_runtime_intent_backfill import (
    RepeatRuntimeIntentBackfillService,
)
from app.services.scheduling import cleanup_runtime_fields, inherit_flags_for_repeat


async def _seed_root_and_child(Session, *, seed: int) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=99200 + seed,
            username=f"repeat-backfill-{seed}",
            full_name=f"Repeat Backfill {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10099200 + seed),
            title=f"Repeat Backfill {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Backfill runtime"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        root_publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
                "nested": {"mode": "stable"},
            },
        )
        root_task_id = int(root_publication.legacy_post_task_id or 0)
        root_task = await session.get(PostTask, root_task_id)
        assert root_task is not None

        child_payload = inherit_flags_for_repeat(
            cleanup_runtime_fields(dict(root_task.payload or {})),
            root_task_id,
        )
        child = PostTask(
            channel_id=int(channel.id),
            status="pending",
            scheduled_at=root_task.scheduled_at + timedelta(hours=1),
            payload=child_payload,
        )
        session.add(child)
        await session.commit()
        await session.refresh(child)
        child_publication = await mirror_legacy_post_task(session, child)
        assert child_publication is not None
        return int(root_publication.id), int(child_publication.id), int(child.id)


async def _strip_child_runtime_metadata(
    session,
    *,
    publication_id: int,
) -> tuple[Publication, ScheduleEntry]:
    publication = await session.get(Publication, publication_id)
    assert publication is not None
    schedule = await session.get(
        ScheduleEntry,
        int(publication.schedule_entry_id or 0),
    )
    assert schedule is not None

    publication_meta = dict(publication.meta or {})
    schedule_meta = dict(schedule.meta or {})
    publication_meta.pop("runtime_options", None)
    publication_meta.pop("repeat_runtime_intent_provenance", None)
    schedule_meta.pop("runtime_options", None)
    schedule_meta.pop("repeat_runtime_intent_provenance", None)
    publication.meta = publication_meta
    schedule.meta = schedule_meta
    await session.commit()
    return publication, schedule


def test_active_backfill_restores_proven_runtime_intent_for_existing_child(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-active-backfill.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            root_publication_id, child_publication_id, _ = await _seed_root_and_child(
                Session,
                seed=1,
            )

            async with Session() as session:
                await _strip_child_runtime_metadata(
                    session,
                    publication_id=child_publication_id,
                )
                batch = await RepeatRuntimeIntentBackfillService(session).backfill_active(
                    limit=10,
                )

            assert batch.scanned == 2
            assert batch.updated == 1
            assert batch.skipped_existing == 1
            assert batch.failures == 0
            assert batch.done is True

            async with Session() as session:
                root = await session.get(Publication, root_publication_id)
                child = await session.get(Publication, child_publication_id)
                assert root is not None and child is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(child.schedule_entry_id or 0),
                )
                assert schedule is not None
                expected = {
                    "autodelete_views": 100,
                    "autodelete_report": True,
                    "nested": {"mode": "stable"},
                }
                assert child.meta["runtime_options"] == expected
                assert schedule.meta["runtime_options"] == expected
                assert child.meta["repeat_runtime_intent_provenance"] is True
                assert schedule.meta["repeat_runtime_intent_provenance"] is True
                assert root.meta["runtime_options"] == expected
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_active_backfill_never_overwrites_one_sided_existing_canonical_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-active-backfill-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, child_publication_id, _ = await _seed_root_and_child(Session, seed=2)

            async with Session() as session:
                child, schedule = await _strip_child_runtime_metadata(
                    session,
                    publication_id=child_publication_id,
                )
                child.meta = {
                    **dict(child.meta or {}),
                    "runtime_options": {"autodelete_views": 999},
                }
                await session.commit()

                batch = await RepeatRuntimeIntentBackfillService(session).backfill_active(
                    limit=10,
                )

            assert batch.scanned == 2
            assert batch.updated == 0
            assert batch.skipped_existing == 2
            assert batch.failures == 0

            async with Session() as session:
                child = await session.get(Publication, child_publication_id)
                assert child is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(child.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert child.meta["runtime_options"] == {"autodelete_views": 999}
                assert "runtime_options" not in dict(schedule.meta or {})
                assert "repeat_runtime_intent_provenance" not in dict(child.meta or {})
                assert "repeat_runtime_intent_provenance" not in dict(schedule.meta or {})
        finally:
            await engine.dispose()

    asyncio.run(run())
