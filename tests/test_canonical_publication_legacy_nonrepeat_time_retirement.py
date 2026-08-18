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
from app.services.canonical_publication_delivery_atomic_handoff_claim import CanonicalPublicationAtomicHandoffClaimService
from app.services.canonical_publication_delivery_authority import set_canonical_publication_delivery_primary_worker
from app.services.canonical_publication_nonrepeat_authority import canonical_publication_delivery_nonrepeat_time_started
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker: pass


async def _seed(Session, *, seed: int, options: dict) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(tg_user_id=223000 + seed, username=f"np-time-{seed}", full_name="NP Time", ui_settings={})
        session.add(owner); await session.flush()
        channel = Channel(tg_chat_id=-(103223000 + seed), title="time", owner_id=int(owner.id), is_active=True)
        session.add(channel); await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(blocks=[{"id":"b1","type":"text","text":"time"}]),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


async def _mark(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as session:
        task = await session.get(PostTask, task_id); assert task is not None
        items = [task]; await scheduler._mark_processing(session, items); return items


def _publish(primary, *, time: bool) -> None:
    set_canonical_publication_delivery_primary_worker(primary, time_autodelete_available=time)


def test_time_fact_requires_live_time_executor() -> None:
    primary = _PrimaryWorker()
    try:
        _publish(primary, time=False); assert canonical_publication_delivery_nonrepeat_time_started() is False
        _publish(primary, time=True); assert canonical_publication_delivery_nonrepeat_time_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)


def test_exact_time_yields_pending_then_atomic_claim_owns_cutover(tmp_path) -> None:
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'np-time.db'}")
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(Session, seed=1, options={"autodelete_seconds": 60})
            _publish(primary, time=True)
            assert await _mark(Session, task_id) == []
            async with Session() as check:
                task = await check.get(PostTask, task_id); publication = await check.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.legacy_post_task_id == task_id
            async with Session() as session:
                result = await CanonicalPublicationAtomicHandoffClaimService(session).claim_linked(
                    publication_id, holder="np-time", ttl_seconds=180, allow_time_autodelete=True
                )
                assert result.outcome == "claimed"
        finally:
            set_canonical_publication_delivery_primary_worker(None); await engine.dispose()
    asyncio.run(run())


def test_time_missing_executor_or_generated_drift_falls_back(tmp_path) -> None:
    async def run():
        for seed, live, drift in ((2, False, False), (3, True, True)):
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/f'np-time-{seed}.db'}")
            primary = _PrimaryWorker()
            try:
                async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
                Session = async_sessionmaker(engine, expire_on_commit=False)
                _, task_id = await _seed(Session, seed=seed, options={"autodelete_seconds": 60})
                if drift:
                    async with Session() as s:
                        task = await s.get(PostTask, task_id); assert task is not None
                        payload = dict(task.payload or {}); payload["autodelete_effective_seconds"] = 60
                        task.payload = payload; await s.commit()
                _publish(primary, time=live)
                items = await _mark(Session, task_id)
                assert len(items) == 1 and items[0].status == "processing"
            finally:
                set_canonical_publication_delivery_primary_worker(None); await engine.dispose()
    asyncio.run(run())


def test_time_slice_keeps_time_pin_on_legacy(tmp_path) -> None:
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'np-time-pin-closed.db'}")
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(Session, seed=4, options={"autodelete_seconds": 60, "pin_on": True})
            _publish(primary, time=True)
            items = await _mark(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None); await engine.dispose()
    asyncio.run(run())
