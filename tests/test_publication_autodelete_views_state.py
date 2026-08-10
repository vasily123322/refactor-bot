from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateConflict,
    PublicationAutodeleteViewStateError,
    PublicationAutodeleteViewStateService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(
    Session,
    *,
    seed_id: int = 1,
    published: bool = True,
    unlink: bool = False,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=89000 + seed_id,
            username=f"views-owner-{seed_id}",
            full_name=f"Views Owner {seed_id}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10089000 + seed_id),
            title=f"Views state {seed_id}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Views state"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options={"autodelete_views": 100},
        )
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert schedule is not None and task is not None
        if published:
            publication.status = "published"
            schedule.status = "completed"
            task.status = "done"
            publication.telegram_message_ids = [99000 + seed_id]
        if unlink:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return int(publication.id), int(schedule.id), int(task.id)


def test_sync_intent_creates_due_state_and_clear_removes_it(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-state-create.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _, _ = await _seed(Session)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                snapshot = await PublicationAutodeleteViewStateService(
                    session
                ).sync_intent(
                    publication_id=publication_id,
                    threshold=100,
                    now=now,
                )
                assert snapshot is not None
                assert snapshot.publication_id == publication_id
                assert snapshot.threshold == 100
                assert snapshot.last_views is None
                assert snapshot.last_checked_at is None
                assert snapshot.next_check_at == now
                await session.commit()

            async with Session() as session:
                removed = await PublicationAutodeleteViewStateService(
                    session
                ).sync_intent(
                    publication_id=publication_id,
                    threshold=False,
                    now=now + timedelta(minutes=1),
                )
                assert removed is None
                await session.commit()

            async with Session() as session:
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unchanged_threshold_preserves_observation_and_schedule(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-state-preserve.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _, _ = await _seed(Session)
            checked = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            next_check = checked + timedelta(minutes=5)

            async with Session() as session:
                service = PublicationAutodeleteViewStateService(session)
                await service.sync_intent(
                    publication_id=publication_id,
                    threshold=100,
                    now=checked - timedelta(minutes=1),
                )
                recorded = await service.record_observation(
                    publication_id=publication_id,
                    expected_threshold=100,
                    views=42,
                    checked_at=checked,
                    next_check_at=next_check,
                )
                assert recorded.last_views == 42
                await session.commit()

            async with Session() as session:
                unchanged = await PublicationAutodeleteViewStateService(
                    session
                ).sync_intent(
                    publication_id=publication_id,
                    threshold=100,
                    now=checked + timedelta(minutes=2),
                )
                assert unchanged is not None
                assert unchanged.last_views == 42
                assert unchanged.last_checked_at == checked
                assert unchanged.next_check_at == next_check
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_threshold_change_resets_observation_and_becomes_due_now(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-state-reset.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _, _ = await _seed(Session)
            checked = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                service = PublicationAutodeleteViewStateService(session)
                await service.sync_intent(
                    publication_id=publication_id,
                    threshold=100,
                    now=checked,
                )
                await service.record_observation(
                    publication_id=publication_id,
                    expected_threshold=100,
                    views=50,
                    checked_at=checked,
                    next_check_at=checked + timedelta(minutes=10),
                )
                await session.commit()

            changed_at = checked + timedelta(minutes=1)
            async with Session() as session:
                changed = await PublicationAutodeleteViewStateService(
                    session
                ).sync_intent(
                    publication_id=publication_id,
                    threshold=200,
                    now=changed_at,
                )
                assert changed is not None
                assert changed.threshold == 200
                assert changed.last_views is None
                assert changed.last_checked_at is None
                assert changed.next_check_at == changed_at
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_observation_conflicts_without_mutating_new_threshold(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-state-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _, _ = await _seed(Session)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                service = PublicationAutodeleteViewStateService(session)
                await service.sync_intent(
                    publication_id=publication_id,
                    threshold=200,
                    now=now,
                )
                await session.commit()

            async with Session() as session:
                with pytest.raises(PublicationAutodeleteViewStateConflict):
                    await PublicationAutodeleteViewStateService(
                        session
                    ).record_observation(
                        publication_id=publication_id,
                        expected_threshold=100,
                        views=150,
                        checked_at=now,
                        next_check_at=now + timedelta(minutes=1),
                    )
                await session.rollback()

            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert state.threshold == 200
                assert state.last_views is None
                assert state.last_checked_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_due_selector_is_bounded_and_includes_linked_and_unlinked_rows(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-state-selector.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            linked_id, _, _ = await _seed(Session, seed_id=1, unlink=False)
            unlinked_id, _, _ = await _seed(Session, seed_id=2, unlink=True)
            future_id, _, _ = await _seed(Session, seed_id=3, unlink=False)
            queued_id, _, _ = await _seed(Session, seed_id=4, published=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                service = PublicationAutodeleteViewStateService(session)
                for publication_id in (linked_id, unlinked_id, future_id, queued_id):
                    await service.sync_intent(
                        publication_id=publication_id,
                        threshold=100,
                        now=now,
                    )
                await service.record_observation(
                    publication_id=future_id,
                    expected_threshold=100,
                    views=10,
                    checked_at=now,
                    next_check_at=now + timedelta(minutes=10),
                )
                await session.commit()

            async with Session() as session:
                batch = await PublicationAutodeleteViewStateService(
                    session
                ).select_due_publication_ids(now=now, limit=1)
                assert batch.publication_ids == (linked_id,)
                assert batch.done is False

                batch = await PublicationAutodeleteViewStateService(
                    session
                ).select_due_publication_ids(now=now, limit=10)
                assert batch.publication_ids == (linked_id, unlinked_id)
                assert batch.done is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_state_validation_rejects_ambiguous_numeric_values(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-state-validation.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _, _ = await _seed(Session)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                service = PublicationAutodeleteViewStateService(session)
                for invalid in (True, -1, "many"):
                    with pytest.raises(PublicationAutodeleteViewStateError):
                        await service.sync_intent(
                            publication_id=publication_id,
                            threshold=invalid,
                            now=now,
                        )
                await service.sync_intent(
                    publication_id=publication_id,
                    threshold=100,
                    now=now,
                )
                for invalid in (True, -1, "many"):
                    with pytest.raises(PublicationAutodeleteViewStateError):
                        await service.record_observation(
                            publication_id=publication_id,
                            expected_threshold=100,
                            views=invalid,
                            checked_at=now,
                            next_check_at=now + timedelta(minutes=1),
                        )
                with pytest.raises(PublicationAutodeleteViewStateError):
                    await service.record_observation(
                        publication_id=publication_id,
                        expected_threshold=100,
                        views=0,
                        checked_at=now,
                        next_check_at=now - timedelta(seconds=1),
                    )
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())
