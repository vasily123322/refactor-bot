from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, ChannelSettings, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_owner_notice_planner import (
    CanonicalPublicationOwnerNoticePlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_published(
    Session,
    *,
    seed: int,
    canonical_attempt: bool,
    filters,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=132000 + seed,
            username=f"owner-notice-authority-{seed}",
            full_name=f"Owner Notice Authority {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100132000 + seed),
            title=f"Owner Notice Authority {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        session.add(
            ChannelSettings(
                channel_id=int(channel.id),
                autosign=None,
                split_rules=None,
                filters=filters,
            )
        )
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Owner notice authority proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            runtime_options={},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        assert task is not None and schedule is not None
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [501]
        publication.result_link = "https://t.me/c/132/501"
        schedule.status = "completed"
        attempt_meta = (
            {"canonical_delivery": True}
            if canonical_attempt
            else {"legacy_post_task_id": int(task.id)}
        )
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[501],
                error=None,
                meta=attempt_meta,
                finished_at=datetime(2026, 8, 11, 12, 1, tzinfo=timezone.utc),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_retired_legacy_attempt_does_not_replay_owner_notice(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'owner-notice-legacy-authority.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(
                Session,
                seed=1,
                canonical_attempt=False,
                filters={"tz": "UTC"},
            )
            async with Session() as session:
                assert await CanonicalPublicationOwnerNoticePlanner(session).plan(
                    publication_id,
                    at=datetime(2026, 8, 11, 12, 2, tzinfo=timezone.utc),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_malformed_channel_settings_filters_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'owner-notice-malformed-settings.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(
                Session,
                seed=2,
                canonical_attempt=True,
                filters=["not", "a", "mapping"],
            )
            async with Session() as session:
                assert await CanonicalPublicationOwnerNoticePlanner(session).plan(
                    publication_id,
                    at=datetime(2026, 8, 11, 12, 2, tzinfo=timezone.utc),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
