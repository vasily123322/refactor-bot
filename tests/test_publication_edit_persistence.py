from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_edit_persistence import (
    PublicationEditConflictError,
    PublicationEditPersistenceError,
    PublicationEditPersistenceService,
)


async def _seed_published(Session) -> tuple[int, int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=71001,
            username="owner",
            full_name="Owner",
            is_premium=False,
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10071001,
            title="Edit persistence",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        channel_id = int(channel.id)

    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Before edit"}]
            ),
            created_by_tg_user_id=71001,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
            repeat_rule={"enabled": True, "seconds": 3600},
        )
        task_id = int(publication.legacy_post_task_id or 0)
        schedule_id = int(publication.schedule_entry_id or 0)
        publication.status = "published"
        publication.telegram_message_ids = [91001]
        await session.commit()
        return channel_id, int(item.id), int(publication.id), schedule_id, task_id


def test_persist_success_appends_revision_without_touching_legacy_transport(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, item_id, publication_id, schedule_id, task_id = await _seed_published(Session)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                legacy_payload_before = dict(task.payload or {})

                result = await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=71001,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "After edit",
                        "_publication_id": 999,
                        "_content_item_id": item_id,
                        "_post_task_id": task_id,
                        "result_ids": [1, 2],
                        "result_link": "https://example.invalid/legacy",
                        "primary_message_id": 123,
                        "repeat_on": False,
                        "repeat_seconds": 5,
                        "repeat_group_id": task_id,
                        "autodeleted": True,
                    },
                    telegram_message_ids=[91001],
                )
                assert result.publication_id == publication_id
                assert result.content_item_id == item_id
                assert result.previous_revision == 1
                assert result.revision == 2
                assert result.telegram_message_ids == (91001,)

            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                task = await session.get(PostTask, task_id)
                revision = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == item_id,
                            ContentRevision.revision == 2,
                        )
                    )
                ).scalar_one()
                assert item is not None and item.current_revision == 2
                assert publication is not None and publication.content_revision == 2
                assert publication.telegram_message_ids == [91001]
                assert schedule is not None and schedule.content_revision == 2
                assert task is not None and dict(task.payload or {}) == legacy_payload_before
                assert revision.source == "telegram_edit"
                assert revision.created_by_tg_user_id == 71001
                assert revision.meta["publication_id"] == publication_id
                assert revision.meta["edited_from_revision"] == 1

                document = dict(revision.document or {})
                blocks = list(document.get("blocks") or [])
                assert blocks and blocks[0]["text"] == "After edit"
                extras = dict((document.get("metadata") or {}).get("legacy_payload_extra") or {})
                blocked = {
                    "_publication_id",
                    "_content_item_id",
                    "_post_task_id",
                    "result_ids",
                    "result_link",
                    "primary_message_id",
                    "repeat_on",
                    "repeat_seconds",
                    "repeat_group_id",
                    "autodeleted",
                }
                assert blocked.isdisjoint(extras)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_persist_success_fails_closed_for_foreign_owner(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit-owner.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, item_id, publication_id, _, _ = await _seed_published(Session)

            async with Session() as session:
                with pytest.raises(PublicationEditPersistenceError):
                    await PublicationEditPersistenceService(session).persist_success(
                        publication_id=publication_id,
                        tg_user_id=71999,
                        expected_revision=1,
                        payload={"type": "text", "text": "Foreign edit"},
                        telegram_message_ids=[91001],
                    )

            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                count = (
                    await session.execute(
                        select(func.count(ContentRevision.id)).where(
                            ContentRevision.content_item_id == item_id
                        )
                    )
                ).scalar_one()
                assert item is not None and item.current_revision == 1
                assert count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_persist_success_rejects_stale_revision_without_partial_write(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, item_id, publication_id, schedule_id, _ = await _seed_published(Session)

            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                assert item is not None
                item.current_revision = 2
                session.add(
                    ContentRevision(
                        content_item_id=item_id,
                        revision=2,
                        document=PostDocument(
                            blocks=[{"id": "b1", "type": "text", "text": "Concurrent edit"}]
                        ).to_dict(),
                        source="editor",
                        created_by_tg_user_id=71001,
                        meta={},
                    )
                )
                await session.commit()

            async with Session() as session:
                with pytest.raises(PublicationEditConflictError):
                    await PublicationEditPersistenceService(session).persist_success(
                        publication_id=publication_id,
                        tg_user_id=71001,
                        expected_revision=1,
                        payload={"type": "text", "text": "Stale edit"},
                        telegram_message_ids=[91001],
                    )

            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                count = (
                    await session.execute(
                        select(func.count(ContentRevision.id)).where(
                            ContentRevision.content_item_id == item_id
                        )
                    )
                ).scalar_one()
                assert item is not None and item.current_revision == 2
                assert publication is not None and publication.content_revision == 1
                assert schedule is not None and schedule.content_revision == 1
                assert count == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_persist_success_requires_confirmed_message_identity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit-message.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, item_id, publication_id, _, _ = await _seed_published(Session)

            async with Session() as session:
                with pytest.raises(PublicationEditPersistenceError):
                    await PublicationEditPersistenceService(session).persist_success(
                        publication_id=publication_id,
                        tg_user_id=71001,
                        expected_revision=1,
                        payload={"type": "text", "text": "No message"},
                        telegram_message_ids=[],
                    )

            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                count = (
                    await session.execute(
                        select(func.count(ContentRevision.id)).where(
                            ContentRevision.content_item_id == item_id
                        )
                    )
                ).scalar_one()
                assert item is not None and item.current_revision == 1
                assert count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
