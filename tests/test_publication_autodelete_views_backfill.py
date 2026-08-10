from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_views_backfill import (
    PublicationAutodeleteViewsBackfillService,
)
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_published(
    Session,
    *,
    seed_id: int,
    threshold: int = 100,
    unlink: bool = False,
    conflict_timer: bool = False,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=94000 + seed_id,
            username=f"views-backfill-owner-{seed_id}",
            full_name=f"Views Backfill Owner {seed_id}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10094000 + seed_id),
            title=f"Views backfill {seed_id}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        options: dict[str, object] = {"autodelete_views": threshold}
        if conflict_timer:
            options["autodelete_seconds"] = 3600
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Backfill"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options=options,
        )
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert schedule is not None and task is not None
        publication.status = "published"
        schedule.status = "completed"
        publication.telegram_message_ids = [99000 + seed_id]
        task.status = "done"
        payload = dict(task.payload or {})
        payload["result_ids"] = [99000 + seed_id]
        payload["autodelete_views"] = threshold
        if conflict_timer:
            payload["autodelete_seconds"] = 3600
        task.payload = payload
        publication_id = int(publication.id)
        if unlink:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return publication_id


def test_backfill_reconstructs_linked_and_unlinked_published_view_intents(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-backfill-basic.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            linked_id = await _seed_published(Session, seed_id=1, threshold=100)
            unlinked_id = await _seed_published(
                Session,
                seed_id=2,
                threshold=250,
                unlink=True,
            )
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                batch = await PublicationAutodeleteViewsBackfillService(
                    session
                ).backfill_published(limit=10, now=now)

            assert batch.scanned == 2
            assert batch.synced == 2
            assert batch.invalid == 0
            assert batch.done is True
            async with Session() as session:
                linked = await session.get(PublicationAutodeleteViewState, linked_id)
                unlinked = await session.get(PublicationAutodeleteViewState, unlinked_id)
                assert linked is not None and linked.threshold == 100
                assert unlinked is not None and unlinked.threshold == 250
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_backfill_clears_stale_state_for_terminal_publication(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-backfill-terminal.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(Session, seed_id=1)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                await PublicationAutodeleteViewStateService(session).sync_intent(
                    publication_id=publication_id,
                    threshold=100,
                    now=now,
                )
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                meta = dict(publication.meta or {})
                meta[AUTODELETE_RUNTIME_META_KEY] = {
                    "mode": "views",
                    "deleted": True,
                    "deleted_at": now.isoformat(),
                }
                publication.meta = meta
                await session.commit()

            async with Session() as session:
                batch = await PublicationAutodeleteViewsBackfillService(
                    session
                ).backfill_published(limit=10, now=now)
                assert batch.scanned == 1
                assert batch.cleared == 1

            async with Session() as session:
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_backfill_rejects_conflicting_timer_and_views_intent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-backfill-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(
                Session,
                seed_id=1,
                conflict_timer=True,
            )

            async with Session() as session:
                batch = await PublicationAutodeleteViewsBackfillService(
                    session
                ).backfill_published(limit=10)
                assert batch.scanned == 1
                assert batch.invalid == 1
                assert batch.cleared == 1

            async with Session() as session:
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_backfill_cursor_is_bounded_and_monotonic(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-backfill-cursor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            first_id = await _seed_published(Session, seed_id=1, threshold=100)
            second_id = await _seed_published(Session, seed_id=2, threshold=200)

            async with Session() as session:
                first = await PublicationAutodeleteViewsBackfillService(
                    session
                ).backfill_published(limit=1)
            assert first.scanned == 1
            assert first.next_cursor == first_id
            assert first.done is False

            async with Session() as session:
                second = await PublicationAutodeleteViewsBackfillService(
                    session
                ).backfill_published(
                    after_publication_id=first.next_cursor,
                    limit=1,
                )
            assert second.scanned == 1
            assert second.next_cursor == second_id
            assert second.done is False

            async with Session() as session:
                final = await PublicationAutodeleteViewsBackfillService(
                    session
                ).backfill_published(
                    after_publication_id=second.next_cursor,
                    limit=1,
                )
            assert final.scanned == 0
            assert final.next_cursor == second_id
            assert final.done is True
        finally:
            await engine.dispose()

    asyncio.run(run())
