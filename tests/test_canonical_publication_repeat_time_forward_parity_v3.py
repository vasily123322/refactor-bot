from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlanner
from app.services.canonical_publication_linked_repeat_time_forward_parity import (
    CanonicalPublicationLinkedRepeatTimeForwardParityService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(
    Session,
    *,
    seed: int,
    options: dict[str, object],
) -> tuple[int, int, tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=248000 + seed,
            username=f"repeat-time-forward-parity-{seed}",
            full_name=f"Repeat Time Forward Parity {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100248000 + seed),
            title=f"Repeat Time Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100348000 + seed),
            title=f"Repeat Time Forward A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100448000 + seed),
            title=f"Repeat Time Forward B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()

        runtime_options = dict(options)
        if runtime_options.get("forward_to") == ["targets"]:
            runtime_options["forward_to"] = [int(target_a.id), int(target_b.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time forward parity"}]
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
        return (
            int(publication.id),
            int(publication.legacy_post_task_id),
            (int(target_a.id), int(target_b.id)),
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
        return CanonicalPublicationLinkedRepeatTimeForwardParityService().prove(
            task=task,
            publication=publication,
            schedule=schedule,
            plan=plan,
        )


async def _assert_pristine(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        attempts = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id
                )
            )
        ).scalars().all()
        assert publication is not None
        assert publication.status == "queued"
        assert publication.legacy_post_task_id == task_id
        assert int(publication.attempt_count or 0) == 0
        assert task is not None and task.status == "pending"
        assert attempts == []
        assert await session.get(PublicationDeliveryLease, publication_id) is None


def test_repeat_time_forward_has_exact_ordered_read_only_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, targets = await _seed(
                Session,
                seed=1,
                options={
                    "silent": True,
                    "autodelete_seconds": 90,
                    "autodelete_report": True,
                    "forward_to": ["targets"],
                },
            )

            proof = await _prove(Session, publication_id, task_id)
            assert proof is not None
            assert proof.repeat_seconds == 60
            assert proof.time_autodelete_seconds == 90
            assert proof.pin_on is False
            assert proof.forward_channel_ids == targets
            assert proof.views_autodelete_threshold is None
            assert proof.autodelete_report is True
            await _assert_pristine(Session, publication_id, task_id)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["forward_to"] = list(reversed(payload["forward_to"]))
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id) is None
            await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_forward_parity_rejects_other_compositions_and_generated_drift(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-parity-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = (
                {"autodelete_seconds": 90},
                {"autodelete_seconds": 90, "pin_on": True, "forward_to": ["targets"]},
                {"autodelete_seconds": 90, "forward_to": ["targets"], "autodelete_views": 10},
            )
            for index, options in enumerate(cases, start=2):
                publication_id, task_id, _ = await _seed(
                    Session,
                    seed=index,
                    options=options,
                )
                assert await _prove(Session, publication_id, task_id) is None
                await _assert_pristine(Session, publication_id, task_id)

            publication_id, task_id, _ = await _seed(
                Session,
                seed=5,
                options={"autodelete_seconds": 90, "forward_to": ["targets"]},
            )
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_effective_seconds"] = 90
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id) is None
            await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
