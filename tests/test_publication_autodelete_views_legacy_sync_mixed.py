from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import app.domain  # noqa: F401 register complete ORM metadata
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentItem
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication
from app.services.publication_autodelete_views_legacy_sync import (
    sync_active_legacy_view_intents,
)
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)


async def _fixture(payload: dict):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        owner = Client(tg_user_id=88001, username=None, full_name=None)
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10088001,
            title="legacy view provenance",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        item = ContentItem(
            channel_id=int(channel.id),
            kind="post",
            status="draft",
            title=None,
            current_revision=1,
            meta={},
        )
        session.add(item)
        await session.flush()
        post = PostTask(
            channel_id=int(channel.id),
            status="pending",
            payload=dict(payload),
            dedupe_key=None,
            scheduled_at=datetime.now(timezone.utc),
            error=None,
        )
        session.add(post)
        await session.flush()
        publication = Publication(
            schedule_entry_id=None,
            content_item_id=int(item.id),
            content_revision=1,
            channel_id=int(channel.id),
            status="queued",
            execution_mode="canonical",
            repeat_source_publication_id=None,
            legacy_post_task_id=int(post.id),
            telegram_message_ids=None,
            result_link=None,
            last_error=None,
            attempt_count=0,
            meta={},
        )
        session.add(publication)
        await session.commit()
        return engine, Session, int(publication.id)


def test_active_mixed_time_views_does_not_create_canonical_view_state() -> None:
    async def run() -> None:
        engine, Session, publication_id = await _fixture(
            {"autodelete_seconds": 60, "autodelete_views": 25}
        )
        try:
            async with Session() as session:
                result = await sync_active_legacy_view_intents(session)
                assert result.scanned == 1
                assert result.synced == 0
                assert result.cleared == 1
                assert result.invalid == 0
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_active_mixed_effective_time_clears_stale_canonical_view_state() -> None:
    async def run() -> None:
        engine, Session, publication_id = await _fixture(
            {
                "autodelete_seconds": 0,
                "autodelete_effective_seconds": 90,
                "autodelete_views": 25,
            }
        )
        try:
            async with Session() as session:
                await PublicationAutodeleteViewStateService(session).sync_intent(
                    publication_id=publication_id,
                    threshold=25,
                )
                await session.commit()

            async with Session() as session:
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is not None
                )
                result = await sync_active_legacy_view_intents(session)
                assert result.scanned == 1
                assert result.synced == 0
                assert result.cleared == 1
                assert result.invalid == 0
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_active_views_only_still_syncs_canonical_threshold() -> None:
    async def run() -> None:
        engine, Session, publication_id = await _fixture(
            {"autodelete_seconds": 0, "autodelete_views": 25}
        )
        try:
            async with Session() as session:
                result = await sync_active_legacy_view_intents(session)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert result.scanned == 1
                assert result.synced == 1
                assert result.cleared == 0
                assert result.invalid == 0
                assert state is not None
                assert int(state.threshold) == 25
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_malformed_timer_intent_fails_closed_without_canonical_view_state() -> None:
    async def run() -> None:
        engine, Session, publication_id = await _fixture(
            {"autodelete_seconds": "not-a-duration", "autodelete_views": 25}
        )
        try:
            async with Session() as session:
                result = await sync_active_legacy_view_intents(session)
                assert result.scanned == 1
                assert result.synced == 0
                assert result.cleared == 1
                assert result.invalid == 1
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
