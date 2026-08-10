from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_views_legacy_sync import (
    sync_active_legacy_view_intents,
)
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_edit_persistence import PublicationEditPersistenceService
from app.workers import publication_reconciler as reconciler_module
from app.workers.publication_reconciler import PublicationReconcilerWorker


async def _seed(Session, *, views: int = 100) -> tuple[int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=91001,
            username="views-producer-owner",
            full_name="Views Producer Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10091001,
            title="Views producer sync",
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
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options={"autodelete_views": views},
        )
        return (
            int(item.id),
            int(publication.id),
            int(publication.schedule_entry_id or 0),
            int(publication.legacy_post_task_id or 0),
        )


def test_active_legacy_sync_uses_current_posttask_threshold_before_reconcile(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-legacy-current.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, publication_id, _, task_id = await _seed(Session, views=100)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_views"] = 250
                task.payload = payload
                await session.commit()

            async with Session() as session:
                result = await sync_active_legacy_view_intents(session, limit=10)
                assert result.scanned == 1
                assert result.synced == 1
                assert result.invalid == 0
                # Existing bridge commit makes the staged indexed state durable in the
                # same reconciliation session.
                await LegacyPublicationBridge(session).reconcile_active(limit=10)

            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert state.threshold == 250
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_active_legacy_threshold_clears_stale_indexed_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-legacy-invalid.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, publication_id, _, task_id = await _seed(Session, views=100)

            async with Session() as session:
                await PublicationAutodeleteViewStateService(session).sync_intent(
                    publication_id=publication_id,
                    threshold=100,
                )
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_views"] = "many"
                task.payload = payload
                await session.commit()

            async with Session() as session:
                result = await sync_active_legacy_view_intents(session, limit=10)
                assert result.scanned == 1
                assert result.invalid == 1
                assert result.cleared == 1
                await LegacyPublicationBridge(session).reconcile_active(limit=10)

            async with Session() as session:
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_postpublication_edit_updates_and_clears_view_state_atomically(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-canonical-edit.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, publication_id, schedule_id, task_id = await _seed(Session, views=100)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and schedule is not None and task is not None
                publication.status = "published"
                schedule.status = "completed"
                publication.telegram_message_ids = [99101]
                task.status = "done"
                await PublicationAutodeleteViewStateService(session).sync_intent(
                    publication_id=publication_id,
                    threshold=100,
                    now=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
                )
                await session.commit()

            async with Session() as session:
                result = await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=91001,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "Views changed",
                        "autodelete_views": 250,
                    },
                    telegram_message_ids=[99101],
                    now=datetime(2026, 8, 10, 12, 1, tzinfo=timezone.utc),
                )
                assert result.revision == 2

            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert state.threshold == 250
                assert state.last_views is None

            async with Session() as session:
                result = await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=91001,
                    expected_revision=2,
                    payload={
                        "type": "text",
                        "text": "Switched to timer",
                        "autodelete_seconds": 3600,
                    },
                    telegram_message_ids=[99101],
                    now=datetime(2026, 8, 10, 12, 2, tzinfo=timezone.utc),
                )
                assert result.revision == 3

            async with Session() as session:
                assert (
                    await session.get(PublicationAutodeleteViewState, publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reconciler_stages_view_intent_before_bridge(monkeypatch) -> None:
    async def run() -> None:
        order: list[str] = []

        class SessionContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(reconciler_module, "AsyncSessionLocal", SessionContext)

        async def mirror(_session, *, limit: int):
            order.append("mirror")
            return 0, 0

        class FakeRepeatRuntimeBackfill:
            def __init__(self, _session) -> None:
                pass

            async def backfill_active(self, *, after_publication_id: int, limit: int):
                order.append("repeat_runtime")
                return SimpleNamespace(
                    scanned=0,
                    updated=0,
                    skipped_existing=0,
                    skipped_unproven=0,
                    failures=0,
                    next_cursor=0,
                    done=True,
                )

        async def sync_views(_session, *, limit: int):
            order.append("views")
            return SimpleNamespace(scanned=1, synced=1, cleared=0, invalid=0)

        class FakeBridge:
            def __init__(self, _session) -> None:
                pass

            async def reconcile_active(self, *, limit: int):
                order.append("bridge")
                return 1

        class FakeRuntimeProjector:
            def __init__(self, _session) -> None:
                pass

            async def backfill_terminal(self, *, after_publication_id: int, limit: int):
                order.append("runtime")
                return SimpleNamespace(scanned=0, updated=0, next_cursor=0, done=True)

        monkeypatch.setattr(reconciler_module, "mirror_unlinked_legacy_tasks", mirror)
        monkeypatch.setattr(
            reconciler_module,
            "RepeatRuntimeIntentBackfillService",
            FakeRepeatRuntimeBackfill,
        )
        monkeypatch.setattr(
            reconciler_module,
            "sync_active_legacy_view_intents",
            sync_views,
        )
        monkeypatch.setattr(reconciler_module, "LegacyPublicationBridge", FakeBridge)
        monkeypatch.setattr(
            reconciler_module,
            "PublicationRuntimeProjector",
            FakeRuntimeProjector,
        )

        worker = PublicationReconcilerWorker(interval_seconds=1, batch_size=10)
        await worker._tick()  # noqa: SLF001 - worker ordering boundary

        assert order == ["mirror", "repeat_runtime", "views", "bridge", "runtime"]

    asyncio.run(run())
