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
from app.services.canonical_publication_delivery_authority import set_canonical_publication_delivery_primary_worker
from app.services.canonical_publication_linked_forward_atomic_handoff import CanonicalPublicationLinkedForwardAtomicHandoffService
from app.services.canonical_publication_nonrepeat_authority import canonical_publication_delivery_nonrepeat_views_forward_started
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler

class _PrimaryWorker: pass

async def _seed(S,seed:int)->tuple[int,int]:
    async with S() as s:
        o=Client(tg_user_id=229000+seed,username=f"np-vf-{seed}",full_name="NP VF",ui_settings={}); s.add(o); await s.flush()
        src=Channel(tg_chat_id=-(104129000+seed),title="src",owner_id=int(o.id),is_active=True)
        a=Channel(tg_chat_id=-(104229000+seed),title="a",owner_id=int(o.id),is_active=True)
        b=Channel(tg_chat_id=-(104329000+seed),title="b",owner_id=int(o.id),is_active=True); s.add_all([src,a,b]); await s.flush()
        item=await ContentRepo(s).create(channel_id=int(src.id),document=PostDocument(blocks=[{"id":"b1","type":"text","text":"vf"}]),created_by_tg_user_id=int(o.tg_user_id))
        p=await LegacyPublicationBridge(s).queue(content_item_id=int(item.id),scheduled_at=datetime.now(timezone.utc)-timedelta(minutes=1),runtime_options={"autodelete_views":25,"forward_to":[int(a.id),int(b.id)]})
        assert p.legacy_post_task_id is not None; return int(p.id),int(p.legacy_post_task_id)

async def _mark(S,tid:int)->list[PostTask]:
    w=Scheduler(S,SimpleNamespace(),repeat_continuation_enabled=False)
    async with S() as s:
        t=await s.get(PostTask,tid); assert t is not None
        items=[t]; await w._mark_processing(s,items); return items

def test_views_forward_fact_requires_forward_sibling(monkeypatch)->None:
    p=_PrimaryWorker()
    try:
        set_canonical_publication_delivery_primary_worker(p,views_autodelete_available=True)
        assert canonical_publication_delivery_nonrepeat_views_forward_started() is True
        monkeypatch.setattr("app.services.canonical_publication_nonrepeat_authority.canonical_publication_delivery_nonrepeat_forward_started",lambda:False)
        assert canonical_publication_delivery_nonrepeat_views_forward_started() is False
    finally: set_canonical_publication_delivery_primary_worker(None)

def test_exact_views_forward_yields_pending_then_forward_atomic_claim(tmp_path)->None:
    async def run():
        e=create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'np-vf.db'}"); p=_PrimaryWorker()
        try:
            async with e.begin() as c: await c.run_sync(Base.metadata.create_all)
            S=async_sessionmaker(e,expire_on_commit=False); pid,tid=await _seed(S,1)
            set_canonical_publication_delivery_primary_worker(p,views_autodelete_available=True)
            assert await _mark(S,tid)==[]
            async with S() as s:
                t=await s.get(PostTask,tid); pub=await s.get(Publication,pid)
                assert t is not None and t.status=="pending"; assert pub is not None and pub.legacy_post_task_id==tid
            async with S() as s:
                out=await CanonicalPublicationLinkedForwardAtomicHandoffService(s).claim_linked_forward(pid,holder="np-vf",ttl_seconds=180,allow_views_autodelete=True)
                assert out.outcome=="claimed"
        finally: set_canonical_publication_delivery_primary_worker(None); await e.dispose()
    asyncio.run(run())

def test_missing_views_forward_fact_preserves_legacy(tmp_path,monkeypatch)->None:
    async def run():
        e=create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'np-vf-closed.db'}"); p=_PrimaryWorker()
        try:
            async with e.begin() as c: await c.run_sync(Base.metadata.create_all)
            S=async_sessionmaker(e,expire_on_commit=False); _,tid=await _seed(S,2)
            set_canonical_publication_delivery_primary_worker(p,views_autodelete_available=True)
            monkeypatch.setattr("app.services.canonical_publication_nonrepeat_scheduler_proof.canonical_publication_delivery_nonrepeat_views_forward_started",lambda:False)
            items=await _mark(S,tid); assert len(items)==1 and items[0].status=="processing"
        finally: set_canonical_publication_delivery_primary_worker(None); await e.dispose()
    asyncio.run(run())
