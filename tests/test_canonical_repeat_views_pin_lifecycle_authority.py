from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_views_lifecycle_authority import (
    CanonicalRepeatViewsLifecycleAuthorityService,
)
from app.services.publication_autodelete_views import PublicationAutodeleteViewsService
from app.services.publication_autodelete_views_state import PublicationAutodeleteViewStateService
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_terminal(Session, *, now: datetime) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=221001,
            username="repeat-views-pin-lifecycle",
            full_name="Repeat Views Pin Lifecycle",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-100221001,
            title="Repeat Views Pin Lifecycle",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "views pin lifecycle"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "pin_on": True,
                "autodelete_views": 17,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=17,
            now=now - timedelta(minutes=1),
        )
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [8801]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[8801],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=now - timedelta(seconds=30),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


class _Views:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return 99


class _Delete:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.calls.append((int(chat_id), int(message_id)))


def test_views_pin_lifecycle_requires_explicit_pin_proof_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-pin-lifecycle.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal(Session, now=now)

            async with Session() as session:
                service = CanonicalRepeatViewsLifecycleAuthorityService(session)
                assert await service.lock_and_prove(publication_id) is None

            async with Session() as session:
                proof = await CanonicalRepeatViewsLifecycleAuthorityService(
                    session
                ).lock_and_prove(publication_id, allow_pin=True)
                assert proof is not None
                assert proof.threshold == 17
                assert proof.telegram_message_ids == (8801,)
                assert proof.runtime_options == {
                    "silent": True,
                    "pin_on": True,
                    "autodelete_views": 17,
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_repeat_views_service_gate_does_not_infer_pin_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-pin-gate-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal(Session, now=now)
            views = _Views()
            delete = _Delete()

            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=views,
                    delete_provider=delete,
                    allow_repeat_views=True,
                ).evaluate_and_delete(publication_id, now=now)
                assert result.outcome == "ineligible"
            assert views.calls == []
            assert delete.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())