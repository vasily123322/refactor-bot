from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlanner
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.canonical_publication_linked_repeat_parity import (
    CanonicalPublicationLinkedRepeatParityService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(
    Session,
    *,
    seed: int,
    pin: bool = False,
    forward: bool = True,
) -> tuple[int, int, tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=226000 + seed,
            username=f"repeat-views-forward-parity-{seed}",
            full_name=f"Repeat Views Forward Parity {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100226000 + seed),
            title=f"Views Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100326000 + seed),
            title=f"Views Forward A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100426000 + seed),
            title=f"Views Forward B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        options: dict[str, object] = {
            "silent": True,
            "autodelete_views": 21,
            "autodelete_report": True,
        }
        if pin:
            options["pin_on"] = True
        ordered = (int(target_b.id), int(target_a.id))
        if forward:
            options["forward_to"] = list(ordered)
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "views forward parity"}]
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
        return (
            int(publication.id),
            int(publication.legacy_post_task_id),
            ordered,
        )


async def _prove(Session, publication_id: int, task_id: int):
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        assert publication is not None and task is not None
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert schedule is not None
        plan = await CanonicalPublicationDeliveryPlanner(session).plan(
            publication_id,
            at=datetime.now(timezone.utc),
        )
        assert plan is not None
        return CanonicalPublicationLinkedRepeatParityService().prove(
            task=task,
            publication=publication,
            schedule=schedule,
            plan=plan,
        )


def test_repeat_views_forward_has_exact_ordered_linked_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, ordered = await _seed(Session, seed=1)

            proof = await _prove(Session, publication_id, task_id)
            assert proof is not None
            assert proof.pin_on is False
            assert proof.forward_channel_ids == ordered
            assert proof.views_autodelete_threshold == 21
            assert proof.autodelete_report is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_forward_order_drift_rejects_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-order-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, ordered = await _seed(Session, seed=2)
            assert await _prove(Session, publication_id, task_id) is not None

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["forward_to"] = list(reversed(ordered))
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_pin_forward_combined_remains_outside_parity_stage(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-pin-forward-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(Session, seed=3, pin=True)
            assert await _prove(Session, publication_id, task_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_forward_parity_does_not_open_existing_strict_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-authority-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(Session, seed=4)
            assert await _prove(Session, publication_id, task_id) is not None

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="views-forward-parity-only",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                )
                assert result.outcome == "claim_unavailable"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
                assert publication.legacy_post_task_id == task_id
                assert task is not None and task.status == "pending"
                assert state is None
        finally:
            await engine.dispose()

    asyncio.run(run())
