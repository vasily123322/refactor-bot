from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.models import PostTask
from app.domain.publishing.models import ScheduleEntry
from app.services.legacy_content_mirror import mirror_legacy_post_task


def test_existing_repeat_mirror_backfills_missing_group_metadata(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-group-backfill.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                root = PostTask(
                    channel_id=61,
                    status="done",
                    scheduled_at=datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc),
                    payload={
                        "type": "text",
                        "text": "Existing repeat root",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "result_ids": [61001],
                    },
                )
                session.add(root)
                await session.commit()
                await session.refresh(root)
                root_id = int(root.id)

                publication = await mirror_legacy_post_task(session, root)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None

                # Simulate a Publication mirrored before canonical repeat-group
                # provenance existed, while the linked transport still carries intent.
                publication.meta = {
                    key: value
                    for key, value in dict(publication.meta or {}).items()
                    if key != "repeat_group_id"
                }
                schedule.meta = {
                    key: value
                    for key, value in dict(schedule.meta or {}).items()
                    if key != "repeat_group_id"
                }
                await session.commit()

                existing = await mirror_legacy_post_task(session, root)
                assert existing is not None
                assert existing.id == publication.id
                await session.refresh(schedule)
                assert existing.meta["repeat_group_id"] == root_id
                assert schedule.meta["repeat_group_id"] == root_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_repeat_mirror_never_overwrites_conflicting_group_metadata(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-group-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                root = PostTask(
                    channel_id=62,
                    status="done",
                    scheduled_at=datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc),
                    payload={
                        "type": "text",
                        "text": "Conflicting repeat root",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "result_ids": [62001],
                    },
                )
                session.add(root)
                await session.commit()
                await session.refresh(root)

                publication = await mirror_legacy_post_task(session, root)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None

                publication.meta = {
                    **dict(publication.meta or {}),
                    "repeat_group_id": 999999,
                }
                schedule.meta = {
                    key: value
                    for key, value in dict(schedule.meta or {}).items()
                    if key != "repeat_group_id"
                }
                await session.commit()

                existing = await mirror_legacy_post_task(session, root)
                assert existing is not None
                await session.refresh(schedule)
                assert existing.meta["repeat_group_id"] == 999999
                assert "repeat_group_id" not in schedule.meta
        finally:
            await engine.dispose()

    asyncio.run(run())
