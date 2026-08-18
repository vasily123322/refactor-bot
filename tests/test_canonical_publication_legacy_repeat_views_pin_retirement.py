from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_repeat_views_pin_started,
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed(Session, *, seed: int, options: dict) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=216000 + seed,
            username=f"repeat-views-pin-{seed}",
            full_name=f"Repeat Views Pin {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(101716000 + seed),
            title=f"Repeat Views Pin {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Views pin {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def _publish(primary: _PrimaryWorker, *, plain: bool = True, pin: bool = True) -> None:
    set_canonical_publication_delivery_primary_worker(
        primary,
        views_autodelete_available=True,
        repeat_continuation_available=True,
        repeat_owner_policy_enforced=True,
        repeat_views_available=plain,
        repeat_views_pin_available=pin,
    )


async def _mark(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        items = [task]
        await scheduler._mark_processing(session, items)
        return items


def test_views_pin_started_requires_plain_views_and_dedicated_fact() -> None:
    primary = _PrimaryWorker()
    try:
        _publish(primary, plain=False, pin=True)
        assert canonical_publication_delivery_repeat_views_pin_started() is False
        _publish(primary, plain=True, pin=False)
        assert canonical_publication_delivery_repeat_views_pin_started() is False
        _publish(primary)
        assert canonical_publication_delivery_repeat_views_pin_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)


def test_scheduler_yields_exact_views_pin_and_handoff_requires_dedicated_allow(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-pin-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                options={"autodelete_views": 50, "pin_on": True},
            )
            _publish(primary)
            assert await _mark(Session, task_id) == []

            async with Session() as check:
                task = await check.get(PostTask, task_id)
                publication = await check.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.status == "queued"

            async with Session() as session:
                insufficient = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-views-pin-insufficient",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                )
                assert insufficient.outcome == "ineligible"

            async with Session() as session:
                claimed = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-views-pin",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                )
                assert claimed.outcome == "claimed"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_views_pin_on_legacy_without_dedicated_or_plain_fact(tmp_path) -> None:
    async def run() -> None:
        for seed, plain, pin in ((2, True, False), (3, False, True)):
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{tmp_path / f'repeat-views-pin-closed-{seed}.db'}"
            )
            primary = _PrimaryWorker()
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                Session = async_sessionmaker(engine, expire_on_commit=False)
                _, task_id = await _seed(
                    Session,
                    seed=seed,
                    options={"autodelete_views": 50, "pin_on": True},
                )
                _publish(primary, plain=plain, pin=pin)
                items = await _mark(Session, task_id)
                assert len(items) == 1 and items[0].status == "processing"
            finally:
                set_canonical_publication_delivery_primary_worker(None)
                await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_time_views_pin_and_report_on_legacy(tmp_path) -> None:
    async def run() -> None:
        cases = (
            (10, {"autodelete_views": 50, "pin_on": True, "autodelete_seconds": 120}),
            (11, {"autodelete_views": 50, "pin_on": True, "autodelete_report": True}),
        )
        for seed, options in cases:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{tmp_path / f'repeat-views-pin-extra-{seed}.db'}"
            )
            primary = _PrimaryWorker()
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                Session = async_sessionmaker(engine, expire_on_commit=False)
                _, task_id = await _seed(Session, seed=seed, options=options)
                _publish(primary)
                items = await _mark(Session, task_id)
                assert len(items) == 1 and items[0].status == "processing"
            finally:
                set_canonical_publication_delivery_primary_worker(None)
                await engine.dispose()

    asyncio.run(run())
