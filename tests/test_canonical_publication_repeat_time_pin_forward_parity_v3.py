from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlanner
from app.services.canonical_publication_linked_repeat_time_pin_forward_parity import (
    CanonicalPublicationLinkedRepeatTimePinForwardParityService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session, seed: int) -> tuple[int, int, tuple[int, int]]:
    async with Session() as session:
        owner = Client(tg_user_id=254000 + seed, username=f"tpf-{seed}", full_name="TPF", ui_settings={})
        session.add(owner); await session.flush()
        source = Channel(tg_chat_id=-(100254000 + seed), title="Source", owner_id=int(owner.id), is_active=True)
        a = Channel(tg_chat_id=-(100354000 + seed), title="A", owner_id=int(owner.id), is_active=True)
        b = Channel(tg_chat_id=-(100454000 + seed), title="B", owner_id=int(owner.id), is_active=True)
        session.add_all([source, a, b]); await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(blocks=[{"id":"b1","type":"text","text":"combined"}]),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc)-timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "pin_on": True,
                "forward_to": [int(a.id), int(b.id)],
                "autodelete_seconds": 90,
            },
        )
        return int(publication.id), int(publication.legacy_post_task_id), (int(a.id), int(b.id))


async def _prove(Session, publication_id: int, task_id: int):
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        plan = await CanonicalPublicationDeliveryPlanner(session).plan(publication_id, at=datetime.now(timezone.utc))
        assert task is not None and schedule is not None and plan is not None
        return CanonicalPublicationLinkedRepeatTimePinForwardParityService().prove(
            task=task, publication=publication, schedule=schedule, plan=plan
        )


def test_exact_combined_parity_and_order_drift(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'combined-parity.db'}")
        try:
            async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, targets = await _seed(Session, 1)
            proof = await _prove(Session, publication_id, task_id)
            assert proof is not None
            assert proof.time_autodelete_seconds == 90
            assert proof.pin_on is True
            assert proof.forward_channel_ids == targets
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                payload = dict(task.payload or {})
                payload["forward_to"] = list(reversed(payload["forward_to"]))
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id) is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_combined_parity_requires_pin_and_exact_forward_together(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'combined-scope.db'}")
        try:
            async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            for seed, mutate in ((2, "pin"), (3, "forward")):
                publication_id, task_id, _ = await _seed(Session, seed)
                async with Session() as session:
                    task = await session.get(PostTask, task_id)
                    payload = dict(task.payload or {})
                    if mutate == "pin": payload["pin_on"] = False
                    else: payload["forward_to"] = []
                    task.payload = payload
                    await session.commit()
                assert await _prove(Session, publication_id, task_id) is None
        finally:
            await engine.dispose()
    asyncio.run(run())
