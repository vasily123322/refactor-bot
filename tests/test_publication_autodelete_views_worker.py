from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseService
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.publication_autodelete_views import PublicationAutodeleteViewsWorker


class FakeViewSource:
    def __init__(
        self,
        values: dict[int, int | None] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.values = dict(values or {})
        self.error = error
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target: str | int, message_id: int) -> int | None:
        self.calls.append((int(target), int(message_id)))
        if self.error is not None:
            raise self.error
        return self.values.get(int(message_id))


class FakeDeleteProvider:
    def __init__(self, errors: dict[int, BaseException] | None = None) -> None:
        self.errors = dict(errors or {})
        self.calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.calls.append((int(chat_id), int(message_id)))
        error = self.errors.get(int(message_id))
        if error is not None:
            raise error
        return True


async def _seed(
    Session,
    *,
    seed_id: int,
    threshold: int = 100,
    message_ids: tuple[int, ...] = (99101,),
    report: bool = False,
    published: bool = True,
    unlink: bool = False,
    now: datetime,
) -> tuple[int, int, int]:
    user_id = 93000 + seed_id
    chat_id = -(10093000 + seed_id)
    async with Session() as session:
        owner = Client(
            tg_user_id=user_id,
            username=f"views-worker-owner-{seed_id}",
            full_name=f"Views Worker Owner {seed_id}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=chat_id,
            title=f"Views worker {seed_id}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        runtime_options: dict[str, object] = {"autodelete_views": threshold}
        if report:
            runtime_options["autodelete_report"] = True
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Views worker"}]
            ),
            created_by_tg_user_id=user_id,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options=runtime_options,
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
            publication.telegram_message_ids = list(message_ids)
            payload = dict(task.payload or {})
            payload["result_ids"] = list(message_ids)
            payload["autodelete_views"] = threshold
            if report:
                payload["autodelete_report"] = True
            task.payload = payload
        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=threshold,
            now=now,
        )
        publication_id = int(publication.id)
        task_id = int(task.id)
        if unlink:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return publication_id, task_id, chat_id


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_shared_lease_requires_explicit_opt_in_for_linked_publication(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-worker-linked-lease.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            linked_id, _, _ = await _seed(Session, seed_id=1, now=now)
            queued_id, _, _ = await _seed(
                Session,
                seed_id=2,
                published=False,
                now=now,
            )

            async with Session() as session:
                service = PublicationAutodeleteLeaseService(session)
                default_linked = await service.acquire(
                    publication_id=linked_id,
                    holder="time-worker",
                )
            assert default_linked is None

            async with Session() as session:
                linked = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=linked_id,
                    holder="views-worker",
                    allow_linked=True,
                )
            assert linked is not None

            async with Session() as session:
                queued = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=queued_id,
                    holder="views-worker",
                    allow_linked=True,
                )
            assert queued is None

            async with Session() as session:
                assert await PublicationAutodeleteLeaseService(session).release(linked)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_deletes_linked_due_publication_and_releases_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-worker-delete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, chat_id = await _seed(
                Session,
                seed_id=1,
                threshold=100,
                message_ids=(99101, 99102),
                now=now,
            )
            views = FakeViewSource({99101: 120, 99102: 130})
            deletes = FakeDeleteProvider()
            worker = PublicationAutodeleteViewsWorker(
                view_source=views,
                delete_provider=deletes,
                session_factory=Session,
                batch_size=10,
            )

            tick = await worker.run_once(now=now)

            assert tick.selected == 1
            assert tick.leased == 1
            assert tick.deleted == 1
            assert tick.failures == 0
            assert tick.release_failures == 0
            assert views.calls == [(chat_id, 99101), (chat_id, 99102)]
            assert deletes.calls == [(chat_id, 99101), (chat_id, 99102)]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
                assert await session.get(PublicationAutodeleteViewState, publication_id) is None
                assert (
                    await PublicationAutodeleteLeaseService(session).current(publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_respects_active_shared_lease_without_transport_calls(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-worker-busy.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed(Session, seed_id=1, now=now)

            async with Session() as session:
                existing = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="other-worker",
                    ttl_seconds=300,
                    allow_linked=True,
                )
            assert existing is not None

            views = FakeViewSource({99101: 500})
            deletes = FakeDeleteProvider()
            worker = PublicationAutodeleteViewsWorker(
                view_source=views,
                delete_provider=deletes,
                session_factory=Session,
            )
            tick = await worker.run_once(now=now)

            assert tick.selected == 1
            assert tick.leased == 0
            assert tick.busy == 1
            assert views.calls == []
            assert deletes.calls == []

            async with Session() as session:
                current = await PublicationAutodeleteLeaseService(session).current(
                    publication_id
                )
                assert current is not None
                assert current.lease_token == existing.lease_token
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_below_threshold_defers_state_and_releases_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-worker-below.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed(Session, seed_id=1, now=now)
            worker = PublicationAutodeleteViewsWorker(
                view_source=FakeViewSource({99101: 99}),
                delete_provider=FakeDeleteProvider(),
                session_factory=Session,
                next_check_seconds=90,
            )

            tick = await worker.run_once(now=now)

            assert tick.below_threshold == 1
            assert tick.deleted == 0
            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert state.last_views == 99
                assert _utc(state.next_check_at) > now
                assert (
                    await PublicationAutodeleteLeaseService(session).current(publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_backs_off_ineligible_due_row_to_prevent_batch_starvation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-worker-ineligible.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed(
                Session,
                seed_id=1,
                report=True,
                now=now,
            )
            views = FakeViewSource({99101: 500})
            deletes = FakeDeleteProvider()
            worker = PublicationAutodeleteViewsWorker(
                view_source=views,
                delete_provider=deletes,
                session_factory=Session,
                ineligible_backoff_seconds=300,
            )

            tick = await worker.run_once(now=now)

            assert tick.ineligible == 1
            assert tick.backoff_failures == 0
            assert views.calls == []
            assert deletes.calls == []
            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert _utc(state.next_check_at) == now.replace(tzinfo=timezone.utc) + __import__("datetime").timedelta(seconds=300)
                assert (
                    await PublicationAutodeleteLeaseService(session).current(publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_retry_releases_lease_and_keeps_nonterminal_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-worker-retry.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed(Session, seed_id=1, now=now)
            worker = PublicationAutodeleteViewsWorker(
                view_source=FakeViewSource({99101: 500}),
                delete_provider=FakeDeleteProvider(
                    {99101: RuntimeError("transient provider failure")}
                ),
                session_factory=Session,
            )

            tick = await worker.run_once(now=now)

            assert tick.retry == 1
            assert tick.deleted == 0
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None and state is not None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
                assert _utc(state.next_check_at) > now
                assert (
                    await PublicationAutodeleteLeaseService(session).current(publication_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_cancellation_leaves_lease_until_expiry(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-worker-cancel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed(Session, seed_id=1, now=now)
            worker = PublicationAutodeleteViewsWorker(
                view_source=FakeViewSource(error=asyncio.CancelledError()),
                delete_provider=FakeDeleteProvider(),
                session_factory=Session,
                lease_ttl_seconds=180,
            )

            with pytest.raises(asyncio.CancelledError):
                await worker.run_once(now=now)

            async with Session() as session:
                current = await PublicationAutodeleteLeaseService(session).current(
                    publication_id
                )
                assert current is not None
                assert current.holder == worker._holder  # noqa: SLF001 - crash boundary
        finally:
            await engine.dispose()

    asyncio.run(run())
