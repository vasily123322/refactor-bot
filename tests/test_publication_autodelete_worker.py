from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.publication_autodelete import PublicationAutodeleteWorker


class FakeProvider:
    def __init__(self, plan: dict[int, BaseException] | None = None) -> None:
        self.plan = dict(plan or {})
        self.calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.calls.append((int(chat_id), int(message_id)))
        failure = self.plan.get(int(message_id))
        if failure is not None:
            raise failure
        return True


async def _seed(
    Session,
    *,
    seed_id: int,
    due_at: datetime,
    message_ids: list[int] | None = None,
) -> tuple[int, int]:
    user_id = 79600 + int(seed_id)
    chat_id = -(10079600 + int(seed_id))
    async with Session() as session:
        owner = Client(
            tg_user_id=user_id,
            username=f"worker-owner-{seed_id}",
            full_name=f"Worker Owner {seed_id}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=chat_id,
            title=f"Autodelete worker {seed_id}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Delete {seed_id}"}]
            ),
            created_by_tg_user_id=user_id,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options={"autodelete_seconds": 3600},
        )
        schedule = await session.get(
            ScheduleEntry, int(publication.schedule_entry_id or 0)
        )
        assert schedule is not None
        assert publication.legacy_post_task_id is None
        publication.status = "published"
        schedule.status = "completed"
        publication.telegram_message_ids = list(message_ids or [99000 + seed_id])
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": {"autodelete_seconds": 3600},
            AUTODELETE_RUNTIME_META_KEY: {
                "scheduled_at": due_at.astimezone(timezone.utc).isoformat(),
                "effective_seconds": 3600,
                "deleted": False,
            },
        }
        await session.commit()
        return int(publication.id), chat_id


def test_worker_deletes_due_publication_and_releases_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-worker-delete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, chat_id = await _seed(
                Session,
                seed_id=1,
                due_at=datetime.now(timezone.utc) - timedelta(hours=1),
                message_ids=[99101, 99102],
            )
            provider = FakeProvider()
            worker = PublicationAutodeleteWorker(
                provider=provider,
                session_factory=Session,
                batch_size=10,
            )

            tick = await worker.run_once()

            assert tick.selected == 1
            assert tick.leased == 1
            assert tick.deleted == 1
            assert tick.ambiguous == 0
            assert tick.failures == 0
            assert tick.release_failures == 0
            assert provider.calls == [(chat_id, 99101), (chat_id, 99102)]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
                assert (
                    await PublicationAutodeleteLeaseService(session).current(publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_skips_active_lease_without_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-worker-busy.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _ = await _seed(
                Session,
                seed_id=1,
                due_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
            async with Session() as session:
                existing = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="other-worker",
                    ttl_seconds=300,
                )
            assert existing is not None

            provider = FakeProvider()
            worker = PublicationAutodeleteWorker(
                provider=provider,
                session_factory=Session,
                batch_size=10,
            )
            tick = await worker.run_once()

            assert tick.selected == 1
            assert tick.leased == 0
            assert tick.busy == 1
            assert provider.calls == []
            async with Session() as session:
                current = await PublicationAutodeleteLeaseService(session).current(
                    publication_id
                )
                assert current is not None
                assert current.lease_token == existing.lease_token
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_ambiguous_releases_lease_and_preserves_pending_runtime(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-worker-ambiguous.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _ = await _seed(
                Session,
                seed_id=1,
                due_at=datetime.now(timezone.utc) - timedelta(hours=1),
                message_ids=[99201],
            )
            provider = FakeProvider({99201: RuntimeError("provider-secret-like-detail")})
            worker = PublicationAutodeleteWorker(
                provider=provider,
                session_factory=Session,
                batch_size=10,
            )

            tick = await worker.run_once()

            assert tick.leased == 1
            assert tick.retry == 0
            assert tick.ambiguous == 1
            assert tick.deleted == 0
            assert tick.release_failures == 0
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is False
                assert (
                    await PublicationAutodeleteLeaseService(session).current(publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_cursor_is_bounded_and_resets_after_complete_scan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-worker-cursor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            await _seed(
                Session,
                seed_id=1,
                due_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
            await _seed(
                Session,
                seed_id=2,
                due_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
            worker = PublicationAutodeleteWorker(
                provider=FakeProvider(),
                session_factory=Session,
                batch_size=1,
            )

            first = await worker.run_once()
            second = await worker.run_once()
            third = await worker.run_once()

            assert first.selected == 1 and first.cursor > 0
            assert second.selected == 1 and second.cursor > first.cursor
            assert third.selected == 0
            assert third.cursor == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_cancellation_leaves_lease_until_expiry(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-worker-cancel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _ = await _seed(
                Session,
                seed_id=1,
                due_at=datetime.now(timezone.utc) - timedelta(hours=1),
                message_ids=[99301],
            )
            provider = FakeProvider({99301: asyncio.CancelledError()})
            worker = PublicationAutodeleteWorker(
                provider=provider,
                session_factory=Session,
                batch_size=10,
                lease_ttl_seconds=180,
            )

            with pytest.raises(asyncio.CancelledError):
                await worker.run_once()

            async with Session() as session:
                current = await PublicationAutodeleteLeaseService(session).current(
                    publication_id
                )
                assert current is not None
                assert current.holder == worker._holder  # noqa: SLF001 - crash boundary
        finally:
            await engine.dispose()

    asyncio.run(run())
