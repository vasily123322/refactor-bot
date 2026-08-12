from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_views import (
    PublicationAutodeleteViewsService,
    PublicationAutodeleteViewsSyncConflict,
)
from app.services.publication_autodelete_views_state import PublicationAutodeleteViewStateService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_terminal_views(
    Session,
    *,
    seed: int,
    now: datetime,
    repeat: bool,
    threshold: int = 17,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=215000 + seed,
            username=f"repeat-views-service-{seed}",
            full_name=f"Repeat Views Service {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100215000 + seed),
            title=f"Repeat Views Service {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"repeat views service {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule=(
                {"enabled": True, "seconds": 60}
                if repeat
                else None
            ),
            runtime_options={"silent": True, "autodelete_views": threshold},
        )
        assert publication.legacy_post_task_id is not None
        task = await session.get(PostTask, int(publication.legacy_post_task_id))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None

        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=threshold,
            now=now - timedelta(minutes=1),
        )

        message_ids = [8400 + seed, 8500 + seed]
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = list(message_ids)
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=list(message_ids),
                error=None,
                meta={"canonical_delivery": True},
                finished_at=now - timedelta(seconds=30),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


class _Views:
    def __init__(self, value: int) -> None:
        self.value = value
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target: str | int, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return self.value


class _DeleteProvider:
    def __init__(self) -> None:
        self.delete_calls: list[tuple[int, int]] = []
        self.report_calls: list[int] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.delete_calls.append((int(chat_id), int(message_id)))

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ) -> None:
        self.report_calls.append(int(chat_id))


class _DriftingViews(_Views):
    def __init__(self, value: int, Session, publication_id: int) -> None:
        super().__init__(value)
        self.Session = Session
        self.publication_id = publication_id

    async def get_message_views(self, target: str | int, message_id: int) -> int:
        value = await super().get_message_views(target, message_id)
        if len(self.calls) == 2:
            async with self.Session() as session:
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == self.publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                attempt.meta = {
                    **dict(attempt.meta or {}),
                    "canonical_delivery": False,
                }
                await session.commit()
        return value


def test_repeat_views_service_is_default_off_before_view_or_delete_calls(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-default-off.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id = await _seed_terminal_views(
                Session,
                seed=1,
                now=now,
                repeat=True,
            )
            views = _Views(99)
            provider = _DeleteProvider()
            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                ).evaluate_and_delete(publication_id, now=now)
            assert result.outcome == "ineligible"
            assert views.calls == []
            assert provider.delete_calls == []
            async with Session() as session:
                assert await session.get(PublicationAutodeleteViewState, publication_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_explicit_gate_observes_and_defers_below_threshold(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-below.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id = await _seed_terminal_views(
                Session,
                seed=2,
                now=now,
                repeat=True,
            )
            views = _Views(16)
            provider = _DeleteProvider()
            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                    allow_repeat_views=True,
                ).evaluate_and_delete(publication_id, now=now)
            assert result.outcome == "below_threshold"
            assert result.observed_views == 16
            assert len(views.calls) == 2
            assert provider.delete_calls == []
            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert int(state.last_views) == 16
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_explicit_gate_revalidates_and_deletes_exact_source_messages(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-delete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id = await _seed_terminal_views(
                Session,
                seed=3,
                now=now,
                repeat=True,
            )
            views = _Views(17)
            provider = _DeleteProvider()
            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                    allow_repeat_views=True,
                ).evaluate_and_delete(publication_id, now=now)
            assert result.outcome == "deleted"
            assert result.deleted_count == 2
            assert [message_id for _, message_id in provider.delete_calls] == [8403, 8503]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                runtime = dict(publication.meta or {}).get(AUTODELETE_RUNTIME_META_KEY)
                assert isinstance(runtime, dict)
                assert runtime.get("mode") == "views"
                assert runtime.get("view_threshold") == 17
                assert runtime.get("deleted") is True
                assert await session.get(PublicationAutodeleteViewState, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_authority_drift_after_observation_blocks_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-predelete-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id = await _seed_terminal_views(
                Session,
                seed=4,
                now=now,
                repeat=True,
            )
            views = _DriftingViews(99, Session, publication_id)
            provider = _DeleteProvider()
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                    await PublicationAutodeleteViewsService(
                        session,
                        view_source=views,
                        delete_provider=provider,
                        allow_repeat_views=True,
                    ).evaluate_and_delete(publication_id, now=now)
            assert len(views.calls) == 2
            assert provider.delete_calls == []
            async with Session() as session:
                assert await session.get(PublicationAutodeleteViewState, publication_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_default_off_repeat_gate_preserves_existing_nonrepeat_views_path(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'nonrepeat-views-default.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id = await _seed_terminal_views(
                Session,
                seed=5,
                now=now,
                repeat=False,
            )
            views = _Views(99)
            provider = _DeleteProvider()
            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=provider,
                ).evaluate_and_delete(publication_id, now=now)
            assert result.outcome == "deleted"
            assert len(provider.delete_calls) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())
