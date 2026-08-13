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
from app.services.canonical_publication_nonrepeat_authority import canonical_publication_delivery_nonrepeat_time_pin_started
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker: pass


async def _seed(Session, seed: int) -> tuple[int, int]:
    async with Session() as s:
        owner = Client(tg_user_id=224000+seed, username=f"np-tp-{seed}", full_name="NP TP", ui_settings={}); s.add(owner); await s.flush()
        ch = Channel(tg_chat_id=-(103324000+seed), title="tp", owner_id=int(owner.id), is_active=True); s.add(ch); await s.flush()
        item = await ContentRepo(s).create(channel_id=int(ch.id), document=PostDocument(blocks=[{"id":"b1","type":"text","text":"tp"}]), created_by_tg_user_id=int(owner.tg_user_id))
        pub = await LegacyPublicationBridge(s).queue(content_item_id=int(item.id), scheduled_at=datetime.now(timezone.utc)-timedelta(minutes=1), runtime_options={"autodelete_seconds":60,"pin_on":True})
        assert pub.legacy_post_task_id is not None; return int(pub.id), int(pub.legacy_post_task_id)


async def _mark(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as s:
        task = await s.get(PostTask, task_id); assert task is not None
        items=[task]; await scheduler._mark_processing(s, items); return items


def test_time_pin_fact_requires_time_and_pin_siblings(monkeypatch) -> None:
    primary=_PrimaryWorker()
    try:
        set_canonical_publication_delivery_primary_worker(primary, time_autodelete_available=True)
        assert canonical_publication_delivery_nonrepeat_time_pin_started() is True
        monkeypatch.setattr("app.services.canonical_publication_nonrepeat_authority.canonical_publication_delivery_nonrepeat_pin_started", lambda: False)
        assert canonical_publication_delivery_nonrepeat_time_pin_started() is False
    finally: set_canonical_publication_delivery_primary_worker(None)


def test_exact_time_pin_yields_pending_then_atomic_claim(tmp_path) -> None:
    async def run():
        engine=create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'np-tp.db'}"); primary=_PrimaryWorker()
        try:
            async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
            Session=async_sessionmaker(engine, expire_on_commit=False); pub_id, task_id=await _seed(Session,1)
            set_canonical_publication_delivery_primary_worker(primary, time_autodelete_available=True)
            assert await _mark(Session,task_id)==[]
            async with Session() as s:
                task=await s.get(PostTask,task_id); pub=await s.get(Publication,pub_id)
                assert task is not None and task.status=="pending"; assert pub is not None and pub.legacy_post_task_id==task_id
            async with Session() as s:
                out=await CanonicalPublicationAtomicHandoffClaimService(s).claim_linked(pub_id,holder="np-tp",ttl_seconds=180,allow_time_autodelete=True)
                assert out.outcome=="claimed"
        finally: set_canonical_publication_delivery_primary_worker(None); await engine.dispose()
    asyncio.run(run())


def test_missing_dedicated_time_pin_fact_falls_back(tmp_path, monkeypatch) -> None:
    async def run():
        engine=create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'np-tp-closed.db'}"); primary=_PrimaryWorker()
        try:
            async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
            Session=async_sessionmaker(engine, expire_on_commit=False); _,task_id=await _seed(Session,2)
            set_canonical_publication_delivery_primary_worker(primary,time_autodelete_available=True)
            monkeypatch.setattr("app.services.canonical_publication_nonrepeat_scheduler_proof.canonical_publication_delivery_nonrepeat_time_pin_started",lambda:False)
            items=await _mark(Session,task_id); assert len(items)==1 and items[0].status=="processing"
        finally: set_canonical_publication_delivery_primary_worker(None); await engine.dispose()
    asyncio.run(run())
