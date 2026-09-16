from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_edit import CanonicalPublicationEditCoordinator
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_edit_persistence import (
    PublicationEditPersistenceError,
    PublicationEditPersistenceService,
)


async def _seed(Session, *, runtime_options: dict) -> tuple[int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=71101,
            username="runtime-owner",
            full_name="Runtime Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10071101,
            title="Runtime options edit",
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
            created_by_tg_user_id=71101,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
            runtime_options=runtime_options,
        )
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        task = await session.get(
            PostTask,
            int(publication.legacy_post_task_id or 0),
        )
        assert schedule is not None
        if task is None:
            task = PostTask(
                channel_id=int(channel.id),
                status="pending",
                payload={"type": "text", "text": "Before"},
                dedupe_key=f"test-edit-runtime-compat:{int(publication.id)}",
                scheduled_at=schedule.scheduled_at,
            )
            session.add(task)
            await session.flush()
            publication.legacy_post_task_id = int(task.id)
        publication.status = "published"
        schedule.status = "completed"
        publication.telegram_message_ids = [91101]
        task.status = "done"
        await session.commit()
        return int(item.id), int(publication.id), int(schedule.id), int(task.id)


def test_edit_adds_runtime_intent_to_publication_and_schedule_not_content(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-runtime-add.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id, publication_id, schedule_id, task_id = await _seed(
                Session,
                runtime_options={"silent": True},
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                legacy_payload_before = dict(task.payload or {})
                result = await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=71101,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "After",
                        "silent": True,
                        "autodelete_seconds": 3600,
                        "autodelete_label": "1 ч",
                        "autodelete_report": True,
                    },
                    telegram_message_ids=[91101],
                )
                assert result.revision == 2

            async with Session() as session:
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
                expected = {
                    "silent": True,
                    "autodelete_seconds": 3600,
                    "autodelete_report": True,
                }
                assert publication is not None
                assert publication.meta["runtime_options"] == expected
                assert schedule is not None
                assert schedule.meta["runtime_options"] == expected
                assert task is not None
                task_payload = dict(task.payload or {})
                for key in ("type", "text", "silent"):
                    assert task_payload.get(key) == legacy_payload_before.get(key)
                assert task_payload["result_ids"] == [91101]
                assert task_payload["result_link"] == "https://t.me/c/71101/91101"
                assert task_payload["autodelete_seconds"] == 3600
                assert task_payload["autodelete_effective_seconds"] == 3600
                assert task_payload["autodelete_report"] is True
                assert task_payload["autodelete_at"]

                document = dict(revision.document or {})
                extras = dict((document.get("metadata") or {}).get("legacy_payload_extra") or {})
                assert "silent" not in extras
                assert "autodelete_seconds" not in extras
                assert "autodelete_label" not in extras
                assert "autodelete_report" not in extras
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_edit_clears_existing_autodelete_intent_but_preserves_other_runtime_options(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-runtime-clear.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, publication_id, schedule_id, _ = await _seed(
                Session,
                runtime_options={
                    "silent": True,
                    "autodelete_seconds": 7200,
                    "autodelete_report": True,
                },
            )

            async with Session() as session:
                await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=71101,
                    expected_revision=1,
                    payload={"type": "text", "text": "Timer cleared", "silent": True},
                    telegram_message_ids=[91101],
                )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert publication is not None and schedule is not None
                assert publication.meta["runtime_options"] == {"silent": True}
                assert schedule.meta["runtime_options"] == {"silent": True}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_edit_runtime_intent_is_fail_closed_and_has_no_partial_revision(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-runtime-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id, publication_id, _, _ = await _seed(Session, runtime_options={})

            async with Session() as session:
                with pytest.raises(
                    PublicationEditPersistenceError,
                    match="mutually exclusive",
                ):
                    await PublicationEditPersistenceService(session).persist_success(
                        publication_id=publication_id,
                        tg_user_id=71101,
                        expected_revision=1,
                        payload={
                            "type": "text",
                            "text": "Invalid runtime",
                            "autodelete_seconds": 3600,
                            "autodelete_views": 100,
                        },
                        telegram_message_ids=[91101],
                    )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                count = (
                    await session.execute(
                        select(func.count(ContentRevision.id)).where(
                            ContentRevision.content_item_id == item_id
                        )
                    )
                ).scalar_one()
                assert publication is not None and publication.content_revision == 1
                assert count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_runtime_intent_is_rejected_before_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-runtime-preflight.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id, publication_id, _, _ = await _seed(Session, runtime_options={})

            coordinator = CanonicalPublicationEditCoordinator(
                provider=object(),  # type: ignore[arg-type] - provider must stay unused
                session_factory=Session,
            )
            with pytest.raises(
                PublicationEditPersistenceError,
                match="mutually exclusive",
            ):
                await coordinator.edit_text_and_persist(
                    publication_id=publication_id,
                    tg_user_id=71101,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "Invalid before provider",
                        "autodelete_seconds": 3600,
                        "autodelete_views": 100,
                    },
                    text="Invalid before provider",
                )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                count = (
                    await session.execute(
                        select(func.count(ContentRevision.id)).where(
                            ContentRevision.content_item_id == item_id
                        )
                    )
                ).scalar_one()
                assert publication is not None and publication.content_revision == 1
                assert count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
