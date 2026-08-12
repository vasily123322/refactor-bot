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
    options: dict[str, object] | None = None,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=238000 + seed,
            username=f"repeat-time-parity-{seed}",
            full_name=f"Repeat Time Parity {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100238000 + seed),
            title=f"Repeat Time Parity Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100338000 + seed),
            title=f"Repeat Time Parity Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()

        runtime_options = (
            {
                "silent": True,
                "autodelete_seconds": 90,
                "autodelete_report": True,
            }
            if options is None
            else dict(options)
        )
        if runtime_options.get("forward_to") == ["target"]:
            runtime_options["forward_to"] = [int(target.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time parity"}]
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


async def _assert_linked_pristine(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        lease = await session.get(PublicationDeliveryLease, publication_id)
        attempts = list(
            (
                await session.execute(
                    select(PublicationAttempt).where(
                        PublicationAttempt.publication_id == publication_id
                    )
                )
            ).scalars().all()
        )
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert publication.legacy_post_task_id == task_id
        assert publication.telegram_message_ids in (None, [])
        assert task is not None and task.status == "pending"
        assert lease is None
        assert attempts == []


def test_plain_repeat_time_has_exact_read_only_linked_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(Session, seed=1)

            proof = await _prove(Session, publication_id, task_id)
            assert proof is not None
            assert proof.repeat_seconds == 60
            assert proof.time_autodelete_seconds == 90
            assert proof.views_autodelete_threshold is None
            assert proof.autodelete_report is True
            assert proof.pin_on is False
            assert proof.forward_channel_ids == ()

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_seconds"] = 91
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_parity_rejects_generated_dual_and_unproven_effect_compositions(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-parity-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = (
                {"autodelete_seconds": 90, "pin_on": True},
                {"autodelete_seconds": 90, "forward_to": ["target"]},
                {"autodelete_seconds": 90, "autodelete_views": 10},
            )
            for index, options in enumerate(cases, start=2):
                publication_id, task_id = await _seed(
                    Session,
                    seed=index,
                    options=options,
                )
                assert await _prove(Session, publication_id, task_id) is None

            publication_id, task_id = await _seed(Session, seed=5)
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_effective_seconds"] = 90
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_parity_cannot_cross_the_destructive_convergence_lock(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-parity-authority-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(Session, seed=6)
            assert await _prove(Session, publication_id, task_id) is not None

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-time-parity-only",
                    ttl_seconds=180,
                    allow_repeat=True,
                    # Existing views-family facts cannot bypass the explicit #330 time lock.
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                    allow_repeat_views_forward=True,
                    allow_repeat_views_pin_forward=True,
                )
                assert result.outcome == "claim_unavailable"

            await _assert_linked_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
