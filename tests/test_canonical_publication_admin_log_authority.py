from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import PublicationAttempt, ScheduleEntry
from app.repositories.admin import AdminConfigRepo
from app.repositories.content import ContentRepo
from app.services.canonical_publication_admin_log_planner import (
    CanonicalPublicationAdminLogPlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_published(
    Session,
    *,
    seed: int,
    canonical_attempt: bool,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=133000 + seed,
            username=f"admin-log-authority-{seed}",
            full_name=f"Admin Log Authority {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100133000 + seed),
            title=f"Admin Log Authority {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        await AdminConfigRepo(session).set_log_chat(-(199000 + seed))
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Admin log authority proof",
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
        publication.telegram_message_ids = [601]
        publication.result_link = "https://t.me/c/133/601"
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[601],
                error=None,
                meta=(
                    {"canonical_delivery": True}
                    if canonical_attempt
                    else {"legacy_post_task_id": int(task.id)}
                ),
                finished_at=datetime(2026, 8, 11, 12, 1, tzinfo=timezone.utc),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id), int(item.id)


def test_retired_legacy_attempt_does_not_replay_admin_log(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'admin-log-legacy-authority.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _item_id = await _seed_published(
                Session,
                seed=1,
                canonical_attempt=False,
            )
            async with Session() as session:
                assert await CanonicalPublicationAdminLogPlanner(session).plan(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_non_post_content_drift_blocks_admin_log_plan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'admin-log-content-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, item_id = await _seed_published(
                Session,
                seed=2,
                canonical_attempt=True,
            )
            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                assert item is not None
                item.kind = "note"
                await session.commit()
                assert await CanonicalPublicationAdminLogPlanner(session).plan(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
