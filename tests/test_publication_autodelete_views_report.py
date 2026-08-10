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

    async def get_message_views(self, target: str | int, message_id: int) -> int | None:
        self.calls.append((int(target), int(message_id)))
        if self.on_call is not None:
            callback = self.on_call
            self.on_call = None
            await callback()
        return self.values.get(int(message_id))


class ReportDeleteProvider:
    def __init__(self) -> None:
        self.delete_calls: list[tuple[int, int]] = []
        self.report_calls: list[dict] = []
        self.delete_errors: dict[int, Exception] = {}
        self.report_error: Exception | None = None
        self.on_delete = None
        self.on_report = None

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.delete_calls.append((int(chat_id), int(message_id)))
        if self.on_delete is not None:
            callback = self.on_delete
            self.on_delete = None
            await callback()
        error = self.delete_errors.get(int(message_id))
        if error is not None:
            raise error
        return True

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ):
        self.report_calls.append(
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "disable_web_page_preview": bool(disable_web_page_preview),
            }
        )
        if self.on_report is not None:
            callback = self.on_report
            self.on_report = None
            await callback()
        if self.report_error is not None:
            raise self.report_error
        return True


async def _seed(
    Session,
    *,
    now: datetime,
    unlink: bool = False,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=92001,
            username="views-report-owner",
            full_name="Views Report Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10092001,
            title="Views report evaluator",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Views report"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        runtime_options = {
            "autodelete_views": 100,
            "autodelete_report": True,
        }
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
        publication.telegram_message_ids = [99101, 99102]
        publication.result_link = "https://t.me/c/92001/99102"
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": dict(runtime_options),
        }
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [99101, 99102],
            "result_link": publication.result_link,
            **runtime_options,
        }
        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=100,
            now=now,
        )
        publication_id = int(publication.id)
        task_id = int(task.id)
        if unlink:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return publication_id, task_id


def test_report_default_remains_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-default-off.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            provider = ReportDeleteProvider()

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "ineligible"
            assert views.calls == []
            assert provider.delete_calls == []
            assert provider.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_explicit_report_sends_only_after_durable_terminal_commit(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-after-commit.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now, unlink=True)
            views = ViewSource({99101: 120, 99102: 150})
            provider = ReportDeleteProvider()

            async def assert_terminal_before_report() -> None:
                async with Session() as session:
                    publication = await session.get(Publication, publication_id)
                    state = await session.get(PublicationAutodeleteViewState, publication_id)
                    assert publication is not None
                    runtime = publication.meta[AUTODELETE_RUNTIME_META_KEY]
                    assert runtime["mode"] == "views"
                    assert runtime["deleted"] is True
                    assert state is None

            provider.on_report = assert_terminal_before_report
            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                    allow_report=True,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "deleted"
            assert result.deleted_count == 2
            assert provider.delete_calls == [(-10092001, 99101), (-10092001, 99102)]
            assert provider.report_calls == [
                {
                    "chat_id": 92001,
                    "text": "🗑️ Пост удалён по просмотрам\nhttps://t.me/c/92001/99102",
                    "disable_web_page_preview": True,
                }
            ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_failure_keeps_terminal_delete_and_never_retries_transport(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            provider = ReportDeleteProvider()
            provider.report_error = RuntimeError("sensitive provider failure")

            async with Session() as session:
                first = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                    allow_report=True,
                ).evaluate_and_delete(publication_id, now=now)
            assert first.outcome == "deleted"
            assert len(provider.delete_calls) == 2
            assert len(provider.report_calls) == 1

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True

            async with Session() as session:
                second = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                    allow_report=True,
                ).evaluate_and_delete(publication_id, now=now)
            assert second.outcome == "already_deleted"
            assert len(provider.delete_calls) == 2
            assert len(provider.report_calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unavailable_only_resolution_never_sends_false_report(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-unavailable.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            provider = ReportDeleteProvider()
            provider.delete_errors[99101] = RuntimeError("message to delete not found")
            provider.delete_errors[99102] = RuntimeError("message_id_invalid")

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                    allow_report=True,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "deleted"
            assert result.deleted_count == 0
            assert result.unavailable_count == 2
            assert provider.report_calls == []
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_flag_race_before_delete_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-predelete-race.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            provider = ReportDeleteProvider()

            async def disable_report() -> None:
                async with Session() as session:
                    task = await session.get(PostTask, task_id)
                    assert task is not None
                    payload = dict(task.payload or {})
                    payload.pop("autodelete_report", None)
                    task.payload = payload
                    await session.commit()

            views.on_call = disable_report
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                    await PublicationAutodeleteViewsService(
                        session,
                        view_source=views,
                        delete_provider=provider,
                        allow_report=True,
                    ).evaluate_and_delete(publication_id, now=now)

            assert provider.delete_calls == []
            assert provider.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_flag_race_after_delete_never_commits_stale_terminal_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-postdelete-race.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(Session, now=now)
            views = ViewSource({99101: 500, 99102: 500})
            provider = ReportDeleteProvider()

            async def disable_report() -> None:
                async with Session() as session:
                    task = await session.get(PostTask, task_id)
                    assert task is not None
                    payload = dict(task.payload or {})
                    payload.pop("autodelete_report", None)
                    task.payload = payload
                    await session.commit()

            provider.on_delete = disable_report
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                    await PublicationAutodeleteViewsService(
                        session,
                        view_source=views,
                        delete_provider=provider,
                        allow_report=True,
                    ).evaluate_and_delete(publication_id, now=now)

            assert provider.delete_calls
            assert provider.report_calls == []
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None and state is not None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
                assert state.threshold == 100
        finally:
            await engine.dispose()

    asyncio.run(run())
