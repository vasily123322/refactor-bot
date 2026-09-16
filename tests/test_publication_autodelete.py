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
from app.services.publication_autodelete import (
    PublicationAutodeleteService,
    PublicationAutodeleteSyncConflict,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


class DeleteUnavailable(Exception):
    pass


class FakeProvider:
    def __init__(self, plan: dict[int, object] | None = None) -> None:
        self.plan = dict(plan or {})
        self.calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.calls.append((int(chat_id), int(message_id)))
        outcome = self.plan.get(int(message_id))
        if isinstance(outcome, BaseException):
            raise outcome
        return True


async def _seed(
    Session,
    *,
    due_at: datetime,
    seed_id: int = 1,
    message_ids: list[int] | None = None,
    unlink: bool = True,
    repeat: bool = False,
    runtime_options: dict | None = None,
) -> tuple[int, int, int, int]:
    user_id = 78000 + int(seed_id)
    chat_id = -(10078000 + int(seed_id))
    async with Session() as session:
        owner = Client(
            tg_user_id=user_id,
            username=f"owner-{seed_id}",
            full_name=f"Owner {seed_id}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=chat_id,
            title=f"Canonical autodelete {seed_id}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Delete me"}]
            ),
            created_by_tg_user_id=user_id,
        )
        options = dict(runtime_options or {"autodelete_seconds": 3600})
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            repeat_rule={"enabled": True, "seconds": 3600} if repeat else None,
            runtime_options=options,
        )
        schedule_id = int(publication.schedule_entry_id or 0)
        schedule = await session.get(ScheduleEntry, schedule_id)
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert schedule is not None

        # Canonical profiles are PostTask-free. Only the guard case that explicitly
        # asks for a linked compatibility transport should synthesize one.
        if task is None and not unlink:
            task = PostTask(
                channel_id=int(channel.id),
                status="pending",
                payload=dict(options),
                dedupe_key=f"test-autodelete-compat:{int(publication.id)}",
                scheduled_at=schedule.scheduled_at,
            )
            session.add(task)
            await session.flush()
            publication.legacy_post_task_id = int(task.id)

        if task is not None:
            task.status = "done"
        publication.status = "published"
        schedule.status = "completed"
        publication.telegram_message_ids = list(message_ids or [98001, 98002])
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": options,
            AUTODELETE_RUNTIME_META_KEY: {
                "scheduled_at": due_at.astimezone(timezone.utc).isoformat(),
                "effective_seconds": 3600,
                "deleted": False,
            },
        }
        task_id = int(task.id) if task is not None else 0
        if unlink and task is not None:
            publication.legacy_post_task_id = None
            await session.delete(task)
        await session.commit()
        return int(channel.id), int(publication.id), schedule_id, task_id


def test_due_unlinked_publication_deletes_without_post_task(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, publication_id, _, task_id = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
            )
            provider = FakeProvider()

            async with Session() as session:
                result = await PublicationAutodeleteService(
                    session, provider=provider
                ).delete_if_due(publication_id, now=now)

            assert result.outcome == "deleted"
            assert result.deleted_count == 2
            assert result.unavailable_count == 0
            assert provider.calls == [(-10078001, 98001), (-10078001, 98002)]

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                runtime = publication.meta[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["deleted"] is True
                assert runtime["deleted_at"] == now.isoformat()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_or_future_publication_never_calls_provider(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-guards.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, linked_id, _, _ = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
                seed_id=1,
                unlink=False,
            )
            _, future_id, _, _ = await _seed(
                Session,
                due_at=now + timedelta(minutes=30),
                seed_id=2,
            )
            provider = FakeProvider()

            async with Session() as session:
                linked = await PublicationAutodeleteService(
                    session, provider=provider
                ).delete_if_due(linked_id, now=now)
                future = await PublicationAutodeleteService(
                    session, provider=provider
                ).delete_if_due(future_id, now=now)

            assert linked.outcome == "ineligible"
            assert future.outcome == "not_due"
            assert provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_or_report_remain_ineligible(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, repeat_id, _, _ = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
                seed_id=1,
                repeat=True,
            )
            _, views_id, _, _ = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
                seed_id=2,
                runtime_options={"autodelete_seconds": 3600, "autodelete_views": 10},
            )
            _, report_id, _, _ = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
                seed_id=3,
                runtime_options={"autodelete_seconds": 3600, "autodelete_report": True},
            )
            provider = FakeProvider()

            async with Session() as session:
                service = PublicationAutodeleteService(session, provider=provider)
                outcomes = [
                    (await service.delete_if_due(value, now=now)).outcome
                    for value in (repeat_id, views_id, report_id)
                ]

            assert outcomes == ["ineligible", "ineligible", "ineligible"]
            assert provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ambiguous_provider_result_is_never_replayed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-ambiguity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, publication_id, _, _ = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
            )
            first = FakeProvider({98002: RuntimeError("temporary provider secret")})

            async with Session() as session:
                result = await PublicationAutodeleteService(
                    session, provider=first
                ).delete_if_due(publication_id, now=now)

            assert result.outcome == "ambiguous"
            assert result.deleted_count == 1
            assert result.ambiguous_count == 1
            assert first.calls == [(-10078001, 98001), (-10078001, 98002)]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is False

            replay = FakeProvider()
            async with Session() as session:
                result = await PublicationAutodeleteService(
                    session, provider=replay
                ).delete_if_due(publication_id, now=now + timedelta(minutes=1))

            assert result.outcome == "ambiguous"
            assert replay.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_already_deleted_is_idempotent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-idempotent.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, publication_id, _, _ = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                meta = dict(publication.meta or {})
                runtime = dict(meta[AUTODELETE_RUNTIME_META_KEY])
                runtime["deleted"] = True
                runtime["deleted_at"] = (now - timedelta(seconds=1)).isoformat()
                meta[AUTODELETE_RUNTIME_META_KEY] = runtime
                publication.meta = meta
                await session.commit()

            provider = FakeProvider()
            async with Session() as session:
                result = await PublicationAutodeleteService(
                    session, provider=provider
                ).delete_if_due(publication_id, now=now)

            assert result.outcome == "already_deleted"
            assert provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_provider_state_change_is_safe_sync_conflict(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, publication_id, _, _ = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
                message_ids=[98001],
            )

            class MutatingProvider:
                async def delete_message(self, *, chat_id: int, message_id: int):
                    async with Session() as mutation_session:
                        publication = await mutation_session.get(Publication, publication_id)
                        assert publication is not None
                        publication.telegram_message_ids = [99999]
                        await mutation_session.commit()
                    return True

            async with Session() as session:
                with pytest.raises(PublicationAutodeleteSyncConflict) as captured:
                    await PublicationAutodeleteService(
                        session,
                        provider=MutatingProvider(),
                    ).delete_if_due(publication_id, now=now)

            assert str(captured.value) == "canonical autodelete sync conflict"
            assert captured.value.__cause__ is None
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ambiguous_metadata_fails_closed_without_provider_calls(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-metadata.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            _, publication_id, schedule_id, _ = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
            )
            provider = FakeProvider()

            async def outcome_after(mutator) -> str:
                async with Session() as session:
                    publication = await session.get(Publication, publication_id)
                    schedule = await session.get(ScheduleEntry, schedule_id)
                    assert publication is not None and schedule is not None
                    mutator(publication, schedule)
                    await session.commit()
                async with Session() as session:
                    return (
                        await PublicationAutodeleteService(
                            session, provider=provider
                        ).delete_if_due(publication_id, now=now)
                    ).outcome

            def bad_report(publication, schedule) -> None:
                meta = dict(publication.meta or {})
                meta["runtime_options"] = {"autodelete_report": "yes"}
                publication.meta = meta

            assert await outcome_after(bad_report) == "ineligible"

            def bad_views(publication, schedule) -> None:
                meta = dict(publication.meta or {})
                meta["runtime_options"] = {"autodelete_views": "many"}
                publication.meta = meta

            assert await outcome_after(bad_views) == "ineligible"

            def bad_deleted(publication, schedule) -> None:
                meta = dict(publication.meta or {})
                meta["runtime_options"] = {"autodelete_seconds": 3600}
                runtime = dict(meta[AUTODELETE_RUNTIME_META_KEY])
                runtime["deleted"] = "yes"
                meta[AUTODELETE_RUNTIME_META_KEY] = runtime
                publication.meta = meta

            assert await outcome_after(bad_deleted) == "ineligible"

            def bad_repeat(publication, schedule) -> None:
                meta = dict(publication.meta or {})
                meta["runtime_options"] = {"autodelete_seconds": 3600}
                runtime = dict(meta[AUTODELETE_RUNTIME_META_KEY])
                runtime["deleted"] = False
                meta[AUTODELETE_RUNTIME_META_KEY] = runtime
                publication.meta = meta
                schedule.repeat_rule = "ambiguous"

            assert await outcome_after(bad_repeat) == "ineligible"
            assert provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
