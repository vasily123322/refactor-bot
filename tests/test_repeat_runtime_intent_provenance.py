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
from app.services.scheduling import cleanup_runtime_fields, inherit_flags_for_repeat


async def _seed_repeat_root(Session, *, seed: int) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=99100 + seed,
            username=f"repeat-runtime-{seed}",
            full_name=f"Repeat Runtime {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10099100 + seed),
            title=f"Repeat Runtime {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Repeat runtime"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
                "nested": {"mode": "stable"},
            },
        )
        return (
            int(publication.id),
            int(publication.legacy_post_task_id or 0),
            int(channel.id),
        )


async def _create_repeat_child(
    session,
    *,
    root_task: PostTask,
    mutate_views: int | None = None,
) -> PostTask:
    payload = inherit_flags_for_repeat(
        cleanup_runtime_fields(dict(root_task.payload or {})),
        int(root_task.id),
    )
    if mutate_views is not None:
        payload["autodelete_views"] = int(mutate_views)
    child = PostTask(
        channel_id=int(root_task.channel_id),
        status="pending",
        scheduled_at=root_task.scheduled_at + timedelta(hours=1),
        payload=payload,
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)
    return child


def test_repeat_child_inherits_canonical_runtime_intent_after_root_transport_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-runtime-retired-root.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            root_publication_id, root_task_id, _ = await _seed_repeat_root(Session, seed=1)

            async with Session() as session:
                root_publication = await session.get(Publication, root_publication_id)
                root_task = await session.get(PostTask, root_task_id)
                assert root_publication is not None and root_task is not None
                root_content_item_id = int(root_publication.content_item_id)
                root_content_revision = int(root_publication.content_revision)
                child = await _create_repeat_child(session, root_task=root_task)
                child_id = int(child.id)

                root_publication.legacy_post_task_id = None
                await session.delete(root_task)
                await session.commit()

                child = await session.get(PostTask, child_id)
                assert child is not None
                child_publication = await mirror_legacy_post_task(session, child)
                assert child_publication is not None
                child_schedule = await session.get(
                    ScheduleEntry,
                    int(child_publication.schedule_entry_id or 0),
                )
                assert child_schedule is not None

                expected = {
                    "autodelete_views": 100,
                    "autodelete_report": True,
                    "nested": {"mode": "stable"},
                }
                assert child_publication.content_item_id == root_content_item_id
                assert child_publication.content_revision == root_content_revision
                assert child_publication.meta["runtime_options"] == expected
                assert child_schedule.meta["runtime_options"] == expected
                assert child_publication.meta["repeat_runtime_intent_provenance"] is True
                assert child_schedule.meta["repeat_runtime_intent_provenance"] is True
                assert child_publication.meta["repeat_group_id"] == root_task_id
                assert child_schedule.meta["repeat_group_id"] == root_task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_child_runtime_intent_fails_closed_on_transport_option_mismatch(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-runtime-mismatch.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            root_publication_id, root_task_id, _ = await _seed_repeat_root(Session, seed=2)

            async with Session() as session:
                root_publication = await session.get(Publication, root_publication_id)
                root_task = await session.get(PostTask, root_task_id)
                assert root_publication is not None and root_task is not None
                child = await _create_repeat_child(
                    session,
                    root_task=root_task,
                    mutate_views=200,
                )
                child_publication = await mirror_legacy_post_task(session, child)
                assert child_publication is not None
                child_schedule = await session.get(
                    ScheduleEntry,
                    int(child_publication.schedule_entry_id or 0),
                )
                assert child_schedule is not None

                assert child_publication.content_item_id == root_publication.content_item_id
                assert child_publication.content_revision == root_publication.content_revision
                assert "runtime_options" not in dict(child_publication.meta or {})
                assert "runtime_options" not in dict(child_schedule.meta or {})
                assert "repeat_runtime_intent_provenance" not in dict(
                    child_publication.meta or {}
                )
                assert "repeat_runtime_intent_provenance" not in dict(
                    child_schedule.meta or {}
                )
                assert child_publication.meta["repeat_group_id"] == root_task_id
                assert child_schedule.meta["repeat_group_id"] == root_task_id
        finally:
            await engine.dispose()

    asyncio.run(run())
