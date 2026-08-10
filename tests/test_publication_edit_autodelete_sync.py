from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_edit import CanonicalPublicationEditCoordinator
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_edit_persistence import (
    PublicationEditPersistenceError,
    PublicationEditPersistenceService,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.canonical_scheduler import Scheduler as CanonicalScheduler


async def _seed(
    Session,
    *,
    runtime_options: dict,
    runtime: dict | None,
    task_payload_updates: dict | None = None,
    unlink: bool = False,
) -> tuple[int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=71201,
            username="sync-owner",
            full_name="Sync Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10071201,
            title="Canonical edit sync",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Before"}]
            ),
            created_by_tg_user_id=71201,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
            runtime_options=runtime_options,
        )
        task_id = int(publication.legacy_post_task_id or 0)
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        task = await session.get(PostTask, task_id)
        assert schedule is not None and task is not None
        publication.status = "published"
        schedule.status = "completed"
        publication.telegram_message_ids = [81101]
        publication.result_link = "https://t.me/c/71201/81101"
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": dict(runtime_options),
        }
        if runtime is not None:
            publication.meta = {
                **dict(publication.meta or {}),
                AUTODELETE_RUNTIME_META_KEY: dict(runtime),
            }
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [81101],
            "result_link": "https://t.me/c/71201/81101",
            **dict(task_payload_updates or {}),
        }
        if unlink:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return int(item.id), int(publication.id), int(schedule.id), task_id


def test_timer_change_syncs_due_state_transport_and_delivery_identity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-autodelete-sync.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            old_due = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, publication_id, _, task_id = await _seed(
                Session,
                runtime_options={"autodelete_seconds": 7200},
                runtime={
                    "effective_seconds": 7200,
                    "scheduled_at": old_due.isoformat(),
                    "deleted": False,
                },
                task_payload_updates={
                    "autodelete_seconds": 7200,
                    "autodelete_effective_seconds": 7200,
                    "autodelete_at": old_due.isoformat(),
                },
            )

            async with Session() as session:
                await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=71201,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "After",
                        "autodelete_seconds": 3600,
                    },
                    telegram_message_ids=[82202],
                    now=now,
                )

            expected_due = (now + timedelta(seconds=3600)).isoformat()
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                assert publication.telegram_message_ids == [82202]
                assert publication.result_link == "https://t.me/c/71201/82202"
                assert publication.meta["runtime_options"] == {
                    "autodelete_seconds": 3600
                }
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY] == {
                    "effective_seconds": 3600,
                    "scheduled_at": expected_due,
                    "deleted": False,
                }
                payload = dict(task.payload or {})
                assert payload["result_ids"] == [82202]
                assert payload["result_link"] == "https://t.me/c/71201/82202"
                assert payload["autodelete_seconds"] == 3600
                assert payload["autodelete_effective_seconds"] == 3600
                assert payload["autodelete_at"] == expected_due
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_text_edit_with_unchanged_timer_preserves_original_due_time(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-autodelete-preserve.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            original_due = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
            _, publication_id, _, task_id = await _seed(
                Session,
                runtime_options={"autodelete_seconds": 7200},
                runtime={
                    "effective_seconds": 7200,
                    "scheduled_at": original_due.isoformat(),
                    "deleted": False,
                },
                task_payload_updates={
                    "autodelete_seconds": 7200,
                    "autodelete_effective_seconds": 7200,
                    "autodelete_at": original_due.isoformat(),
                },
            )

            async with Session() as session:
                await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=71201,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "Text only",
                        "autodelete_seconds": 7200,
                    },
                    telegram_message_ids=[81101],
                    now=datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc),
                )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["scheduled_at"] == (
                    original_due.isoformat()
                )
                assert dict(task.payload or {})["autodelete_at"] == original_due.isoformat()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_timer_clear_removes_generated_state_and_stale_local_timer_is_not_due(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-autodelete-clear.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            old_due = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, publication_id, _, task_id = await _seed(
                Session,
                runtime_options={"autodelete_seconds": 3600},
                runtime={
                    "effective_seconds": 3600,
                    "scheduled_at": old_due.isoformat(),
                    "deleted": False,
                },
                task_payload_updates={
                    "autodelete_seconds": 3600,
                    "autodelete_effective_seconds": 3600,
                    "autodelete_at": old_due.isoformat(),
                },
            )

            async with Session() as session:
                await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=71201,
                    expected_revision=1,
                    payload={"type": "text", "text": "Timer cleared"},
                    telegram_message_ids=[81101],
                    now=datetime(2026, 8, 10, 11, 30, tzinfo=timezone.utc),
                )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
                payload = dict(task.payload or {})
                for key in (
                    "autodelete_seconds",
                    "autodelete_effective_seconds",
                    "autodelete_at",
                ):
                    assert key not in payload

                state = await CanonicalScheduler._delayed_delete_state(  # noqa: SLF001
                    object(),
                    session,
                    post_id=task_id,
                    now=datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc),
                )
                assert state is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_local_timer_uses_current_due_ids_and_report_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'stale-local-timer-state.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            due = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, _, _, task_id = await _seed(
                Session,
                runtime_options={"autodelete_seconds": 3600},
                runtime={
                    "effective_seconds": 3600,
                    "scheduled_at": due.isoformat(),
                    "deleted": False,
                },
                task_payload_updates={
                    "result_ids": [83303],
                    "result_link": "https://t.me/c/71201/83303",
                    "autodelete_seconds": 3600,
                    "autodelete_effective_seconds": 3600,
                    "autodelete_at": due.isoformat(),
                    "autodelete_report": True,
                },
            )

            async with Session() as session:
                future = await CanonicalScheduler._delayed_delete_state(  # noqa: SLF001
                    object(),
                    session,
                    post_id=task_id,
                    now=due - timedelta(seconds=1),
                )
                assert future is None
                due_state = await CanonicalScheduler._delayed_delete_state(  # noqa: SLF001
                    object(),
                    session,
                    post_id=task_id,
                    now=due,
                )
                assert due_state is not None
                assert due_state.chat_id == -10071201
                assert due_state.message_ids == (83303,)
                assert due_state.report is True
                assert due_state.result_link == "https://t.me/c/71201/83303"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unlinked_views_or_report_are_rejected_before_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'unlinked-autodelete-capability.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, publication_id, _, _ = await _seed(
                Session,
                runtime_options={},
                runtime=None,
                unlink=True,
            )
            coordinator = CanonicalPublicationEditCoordinator(
                provider=object(),  # type: ignore[arg-type] - provider must stay unused
                session_factory=Session,
            )

            with pytest.raises(PublicationEditPersistenceError, match="requires legacy"):
                await coordinator.edit_text_and_persist(
                    publication_id=publication_id,
                    tg_user_id=71201,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "Unsupported views",
                        "autodelete_views": 100,
                    },
                    text="Unsupported views",
                )
            with pytest.raises(PublicationEditPersistenceError, match="requires legacy"):
                await coordinator.edit_text_and_persist(
                    publication_id=publication_id,
                    tg_user_id=71201,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "Unsupported report",
                        "autodelete_seconds": 3600,
                        "autodelete_report": True,
                    },
                    text="Unsupported report",
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
