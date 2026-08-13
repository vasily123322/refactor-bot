from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_repeat_views_started,
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed(Session, *, seed: int, runtime_options: dict) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=215000 + seed,
            username=f"repeat-views-retirement-{seed}",
            full_name=f"Repeat Views Retirement {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(101615000 + seed),
            title=f"Repeat Views Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Repeat views {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=runtime_options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def _publish_primary(primary: _PrimaryWorker, *, repeat_views: bool = True) -> None:
    set_canonical_publication_delivery_primary_worker(
        primary,
        views_autodelete_available=True,
        repeat_continuation_available=True,
        repeat_owner_policy_enforced=True,
        repeat_views_available=repeat_views,
    )


async def _mark_one(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        items = [task]
        await scheduler._mark_processing(session, items)
        return items


def test_repeat_views_started_requires_started_views_dependency_and_dedicated_fact() -> None:
    primary = _PrimaryWorker()
    try:
        set_canonical_publication_delivery_primary_worker(
            primary,
            views_autodelete_available=False,
            repeat_continuation_available=True,
            repeat_owner_policy_enforced=True,
            repeat_views_available=True,
        )
        assert canonical_publication_delivery_repeat_views_started() is False
        _publish_primary(primary, repeat_views=False)
        assert canonical_publication_delivery_repeat_views_started() is False
        _publish_primary(primary)
        assert canonical_publication_delivery_repeat_views_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)


def test_scheduler_yields_plain_repeat_views_before_atomic_handoff(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                runtime_options={"autodelete_views": 50},
            )
            _publish_primary(primary)

            assert await _mark_one(Session, task_id) == []
            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.status == "queued"
                assert publication.legacy_post_task_id == task_id

            async with Session() as canonical_session:
                insufficient = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-views-missing-dedicated",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                )
                assert insufficient.outcome == "ineligible"

            async with Session() as canonical_session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-views-retirement",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                )
                assert transfer.outcome == "claimed"
                assert transfer.claim is not None

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None and publication.status == "sending"
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_plain_repeat_views_on_legacy_without_dedicated_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-no-dedicated.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=2,
                runtime_options={"autodelete_views": 50},
            )
            _publish_primary(primary, repeat_views=False)
            items = await _mark_one(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_other_views_compositions_and_time_views_on_legacy(tmp_path) -> None:
    async def run() -> None:
        cases = (
            (10, {"autodelete_views": 50, "pin_on": True}),
            (11, {"autodelete_views": 50, "autodelete_seconds": 120}),
            (12, {"autodelete_views": 50, "autodelete_report": True}),
            (13, {"autodelete_views": 50, "autodelete_effective_seconds": 120}),
        )
        for seed, options in cases:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{tmp_path / f'repeat-views-closed-{seed}.db'}"
            )
            primary = _PrimaryWorker()
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                Session = async_sessionmaker(engine, expire_on_commit=False)
                _, task_id = await _seed(Session, seed=seed, runtime_options=options)
                _publish_primary(primary)
                items = await _mark_one(Session, task_id)
                assert len(items) == 1 and items[0].status == "processing"
            finally:
                set_canonical_publication_delivery_primary_worker(None)
                await engine.dispose()

    asyncio.run(run())
