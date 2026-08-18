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
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_linked_forward_atomic_handoff import (
    CanonicalPublicationLinkedForwardAtomicHandoffService,
)
from app.services.canonical_publication_nonrepeat_authority import (
    canonical_publication_delivery_nonrepeat_pin_forward_started,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed(Session, *, seed: int) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(tg_user_id=222000 + seed, username=f"np-pf-{seed}", full_name="NP PF", ui_settings={})
        session.add(owner)
        await session.flush()
        source = Channel(tg_chat_id=-(103022000 + seed), title="PF source", owner_id=int(owner.id), is_active=True)
        target = Channel(tg_chat_id=-(103122000 + seed), title="PF target", owner_id=int(owner.id), is_active=True)
        session.add_all([source, target])
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(blocks=[{"id": "b1", "type": "text", "text": "pin forward"}]),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={"pin_on": True, "forward_to": [int(target.id)]},
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


async def _mark(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        items = [task]
        await scheduler._mark_processing(session, items)
        return items


def test_pin_forward_fact_requires_both_siblings(monkeypatch) -> None:
    primary = _PrimaryWorker()
    try:
        set_canonical_publication_delivery_primary_worker(primary)
        assert canonical_publication_delivery_nonrepeat_pin_forward_started() is True
        monkeypatch.setattr(
            "app.services.canonical_publication_nonrepeat_authority."
            "canonical_publication_delivery_nonrepeat_pin_started",
            lambda: False,
        )
        assert canonical_publication_delivery_nonrepeat_pin_forward_started() is False
    finally:
        set_canonical_publication_delivery_primary_worker(None)


def test_exact_pin_forward_yields_then_forward_atomic_claim_owns_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'np-pf.db'}")
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(Session, seed=1)
            set_canonical_publication_delivery_primary_worker(primary)
            assert await _mark(Session, task_id) == []
            async with Session() as check:
                task = await check.get(PostTask, task_id)
                publication = await check.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.legacy_post_task_id == task_id
            async with Session() as session:
                result = await CanonicalPublicationLinkedForwardAtomicHandoffService(session).claim_linked_forward(
                    publication_id,
                    holder="np-pf",
                    ttl_seconds=180,
                )
                assert result.outcome == "claimed"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_missing_combined_fact_falls_back_to_legacy(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'np-pf-closed.db'}")
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(Session, seed=2)
            set_canonical_publication_delivery_primary_worker(primary)
            monkeypatch.setattr(
                "app.services.canonical_publication_nonrepeat_scheduler_proof."
                "canonical_publication_delivery_nonrepeat_pin_forward_started",
                lambda: False,
            )
            items = await _mark(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())
