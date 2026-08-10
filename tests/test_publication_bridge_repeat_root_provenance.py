from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_content(Session, *, seed: int) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=99000 + seed,
            username=f"repeat-root-{seed}",
            full_name=f"Repeat Root {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10099000 + seed),
            title=f"Repeat Root {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Repeat root"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        return int(item.id)


def test_queue_repeat_root_persists_group_without_overwriting_runtime_options(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'queue-repeat-root.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id = await _seed_content(Session, seed=1)

            async with Session() as session:
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item_id,
                    scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
                    repeat_rule={"enabled": True, "seconds": 3600},
                    runtime_options={
                        "autodelete_views": 100,
                        "autodelete_report": True,
                    },
                )
                task_id = int(publication.legacy_post_task_id or 0)
                assert task_id > 0
                task = await session.get(PostTask, task_id)
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert task is not None and schedule is not None

                expected_options = {
                    "autodelete_views": 100,
                    "autodelete_report": True,
                }
                assert publication.meta["repeat_group_id"] == task_id
                assert schedule.meta["repeat_group_id"] == task_id
                assert publication.meta["runtime_options"] == expected_options
                assert schedule.meta["runtime_options"] == expected_options
                assert schedule.meta["legacy_post_task_id"] == task_id
                assert schedule.repeat_rule == {"enabled": True, "seconds": 3600}
                assert task.payload["repeat_on"] is True
                assert task.payload["repeat_seconds"] == 3600
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_queue_nonrepeat_does_not_invent_repeat_group(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'queue-nonrepeat-root.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id = await _seed_content(Session, seed=2)

            async with Session() as session:
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item_id,
                    scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
                )
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert "repeat_group_id" not in dict(publication.meta or {})
                assert "repeat_group_id" not in dict(schedule.meta or {})
                assert schedule.repeat_rule == {}
        finally:
            await engine.dispose()

    asyncio.run(run())
