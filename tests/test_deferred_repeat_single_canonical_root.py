from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.routers.utils import post_payload as post_payload_helpers
from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.posting import PostingService


class _Bot:
    pass


async def _new_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_deferred_pro_repeat_creates_one_canonical_root_without_legacy_preseed() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        original_factory = post_payload_helpers.AsyncSessionLocal
        post_payload_helpers.AsyncSessionLocal = Session
        try:
            async with Session() as session:
                owner = Client(
                    tg_user_id=9_940_001,
                    username="repeatroot",
                    full_name="Repeat Root Fixture",
                    is_premium=True,
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-1_009_940_000_001,
                    title="Repeat root",
                    owner_id=int(owner.id),
                )
                session.add(channel)
                await session.commit()
                channel_id = int(channel.id)

            when = datetime.now(timezone.utc) + timedelta(hours=1)
            payload = {
                "type": "text",
                "text": "One canonical deferred repeat root",
                "repeat_on": True,
                "repeat_seconds": 3600,
            }
            service = PostingService(_Bot(), Session)
            root = await service.schedule(
                channel_id,
                payload,
                when,
                dedupe_key="deferred-repeat-root",
            )

            # This helper remains imported by the legacy UI while canonical
            # continuation owns every successor after the deferred root.
            await post_payload_helpers._schedule_next_repeat_if_pro(
                service,
                channel_id,
                payload,
                {"repeat_on": True, "repeat_seconds": 3600},
                when,
            )

            async with Session() as session:
                tasks = list((await session.execute(select(PostTask))).scalars().all())
                publications = list(
                    (await session.execute(select(Publication))).scalars().all()
                )
                schedules = list(
                    (await session.execute(select(ScheduleEntry))).scalars().all()
                )

                assert [int(task.id) for task in tasks] == [int(root.id)]
                assert len(publications) == 1
                assert len(schedules) == 1
                publication = publications[0]
                schedule = schedules[0]
                assert publication.legacy_post_task_id == int(root.id)
                assert publication.schedule_entry_id == int(schedule.id)
                assert publication.status == "queued"
                assert schedule.status == "pending"
                assert dict(publication.meta or {}).get("repeat_group_id") == int(root.id)
                assert dict(schedule.meta or {}).get("repeat_group_id") == int(root.id)
        finally:
            post_payload_helpers.AsyncSessionLocal = original_factory
            await engine.dispose()

    asyncio.run(run())
