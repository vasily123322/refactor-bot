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
from app.services.publication_autodelete_views import (
    PublicationAutodeleteViewsService,
    PublicationAutodeleteViewsSyncConflict,
)
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


class ViewSource:
    def __init__(self, values: dict[int, int | None]) -> None:
        self.values = dict(values)
        self.calls: list[tuple[int, int]] = []
        self.on_call = None
        self.error: BaseException | None = None

    async def get_message_views(self, target: str | int, message_id: int) -> int | None:
        self.calls.append((int(target), int(message_id)))
        if self.on_call is not None:
            callback = self.on_call
            self.on_call = None
            await callback()
        if self.error is not None:
            raise self.error
        return self.values.get(int(message_id))


class DeleteProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.errors: dict[int, Exception] = {}
        self.on_call = None

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.calls.append((int(chat_id), int(message_id)))
        if self.on_call is not None:
            callback = self.on_call
            self.on_call = None
            await callback()
        error = self.errors.get(int(message_id))
        if error is not None:
            raise error
        return True


async def _seed(
    Session,
    *,
    threshold: int = 100,
    message_ids: tuple[int, ...] = (99101, 99102),
    report: bool = False,
    unlink: bool = False,
    now: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=92001,
            username="views-evaluator-owner",
            full_name="Views Evaluator Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10092001,
            title="Views evaluator",
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
                blocks=[{"id": "b1", "type": "text", "text": "Views evaluator"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
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
        publication.status = "published"
        schedule.status = "completed"
        publication.telegram_message_ids = list(message_ids)
        task.status = "done"
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
        task_id = int(task.id)
        publication_id = int(publication.id)
        if unlink:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return publication_id, task_id


def test_album_threshold_uses_minimum_view_count_and_records_observation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-below.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({99101: 250, 99102: 99})
            deletes = DeleteProvider()

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=deletes,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "below_threshold"
            assert result.observed_views == 99
            assert deletes.calls == []
            assert views.calls == [(-10092001, 99101), (-10092001, 99102)]
            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert state.last_views == 99
                assert state.last_checked_at == now
                assert state.next_check_at > now
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_view_count_defers_without_delete_or_false_observation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-missing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: None})
            deletes = DeleteProvider()

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=deletes,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "deferred"
            assert result.observed_views is None
            assert deletes.calls == []
            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert state.last_views is None
                assert state.last_checked_at is None
                assert state.next_check_at > now
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reached_threshold_deletes_all_messages_and_records_single_terminal_truth(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-delete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({99101: 120, 99102: 150})
            deletes = DeleteProvider()

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=deletes,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "deleted"
            assert result.observed_views == 120
            assert result.deleted_count == 2
            assert deletes.calls == [(-10092001, 99101), (-10092001, 99102)]
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None
                assert state is None
                runtime = publication.meta[AUTODELETE_RUNTIME_META_KEY]
                assert runtime == {
                    "mode": "views",
                    "view_threshold": 100,
                    "observed_views": 120,
                    "deleted": True,
                    "deleted_at": now.isoformat(),
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_current_linked_threshold_is_revalidated_after_view_read_before_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-predelete-race.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            deletes = DeleteProvider()

            async def mutate_threshold() -> None:
                async with Session() as session:
                    task = await session.get(PostTask, task_id)
                    assert task is not None
                    payload = dict(task.payload or {})
                    payload["autodelete_views"] = 600
                    task.payload = payload
                    await session.commit()

            views.on_call = mutate_threshold
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                    await PublicationAutodeleteViewsService(
                        session,
                        view_source=views,
                        delete_provider=deletes,
                    ).evaluate_and_delete(publication_id, now=now)

            assert deletes.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_provider_threshold_race_never_marks_stale_delete_terminal(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-postdelete-race.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            deletes = DeleteProvider()

            async def mutate_threshold() -> None:
                async with Session() as session:
                    task = await session.get(PostTask, task_id)
                    assert task is not None
                    payload = dict(task.payload or {})
                    payload["autodelete_views"] = 600
                    task.payload = payload
                    await session.commit()

            deletes.on_call = mutate_threshold
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                    await PublicationAutodeleteViewsService(
                        session,
                        view_source=views,
                        delete_provider=deletes,
                    ).evaluate_and_delete(publication_id, now=now)

            assert deletes.calls
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None and state is not None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
                assert state.threshold == 100
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_partial_retryable_delete_stays_nonterminal_and_is_deferred(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-partial-retry.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            deletes = DeleteProvider()
            deletes.errors[99102] = RuntimeError("transient provider failure")

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=deletes,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "retry"
            assert result.deleted_count == 1
            assert result.retryable_count == 1
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None and state is not None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
                assert state.next_check_at > now
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_terminal_unavailable_messages_resolve_as_deleted(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-unavailable.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            deletes = DeleteProvider()
            deletes.errors[99101] = RuntimeError("message to delete not found")
            deletes.errors[99102] = RuntimeError("message_id_invalid")

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=deletes,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "deleted"
            assert result.deleted_count == 0
            assert result.unavailable_count == 2
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
                assert await session.get(PublicationAutodeleteViewState, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_enabled_views_remain_fail_closed_without_touching_transport(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-ineligible.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, report=True, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            deletes = DeleteProvider()

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=deletes,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "ineligible"
            assert views.calls == []
            assert deletes.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unlinked_publication_uses_canonical_runtime_options(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-unlinked.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, unlink=True, now=now)
            views = ViewSource({99101: 101, 99102: 105})
            deletes = DeleteProvider()

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=deletes,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "deleted"
            assert deletes.calls == [(-10092001, 99101), (-10092001, 99102)]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_view_source_cancellation_propagates_without_state_mutation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-cancel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({})
            views.error = asyncio.CancelledError()
            deletes = DeleteProvider()

            async with Session() as session:
                with pytest.raises(asyncio.CancelledError):
                    await PublicationAutodeleteViewsService(
                        session,
                        view_source=views,
                        delete_provider=deletes,
                    ).evaluate_and_delete(publication_id, now=now)

            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert state.last_views is None
                assert state.next_check_at == now
        finally:
            await engine.dispose()

    asyncio.run(run())
