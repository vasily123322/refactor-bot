from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlanner
from app.services.canonical_publication_linked_repeat_parity import (
    CanonicalPublicationLinkedRepeatParityService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session, *, pin_on: bool = False):
    async with Session() as session:
        owner = Client(
            tg_user_id=211001,
            username="repeat-forward-parity",
            full_name="Repeat Forward Parity",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-100211001,
            title="Repeat Forward Source",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-100311001,
            title="Repeat Forward A",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-100411001,
            title="Repeat Forward B",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat + forward"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        options = {
            "silent": True,
            "forward_to": [int(target_b.id), int(target_a.id)],
        }
        if pin_on:
            options["pin_on"] = True
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        plan = await CanonicalPublicationDeliveryPlanner(session).plan(
            int(publication.id),
            at=datetime.now(timezone.utc),
        )
        assert plan is not None
        return (
            int(publication.id),
            int(task.id),
            int(target_b.id),
            int(target_a.id),
            plan,
        )


async def _prove(Session, publication_id: int, task_id: int, plan):
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        assert publication is not None and task is not None
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert schedule is not None
        return CanonicalPublicationLinkedRepeatParityService().prove(
            task=task,
            publication=publication,
            schedule=schedule,
            plan=plan,
        )


def test_repeat_forward_parity_preserves_exact_ordered_internal_channel_ids(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-forward-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, target_b, target_a, plan = await _seed(Session)

            proof = await _prove(Session, publication_id, task_id, plan)
            assert proof is not None
            assert proof.pin_on is False
            assert proof.forward_channel_ids == (target_b, target_a)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["forward_to"] = [target_a, target_b]
                task.payload = payload
                await session.commit()

            assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_pin_forward_parity_requires_both_exact_effects(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-pin-forward-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, target_b, target_a, plan = await _seed(
                Session,
                pin_on=True,
            )

            proof = await _prove(Session, publication_id, task_id, plan)
            assert proof is not None
            assert proof.pin_on is True
            assert proof.forward_channel_ids == (target_b, target_a)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["pin_on"] = False
                task.payload = payload
                await session.commit()

            assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
