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
from app.services.canonical_publication_linked_repeat_time_pin_parity import (
    CanonicalPublicationLinkedRepeatTimePinParityService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(
    Session,
    *,
    seed: int,
    options: dict[str, object],
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=242000 + seed,
            username=f"repeat-time-pin-parity-{seed}",
            full_name=f"Repeat Time Pin Parity {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100242000 + seed),
            title=f"Repeat Time Pin Parity {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100342000 + seed),
            title=f"Repeat Time Pin Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([channel, target])
        await session.commit()

        runtime_options = dict(options)
        if runtime_options.get("forward_to") == ["target"]:
            runtime_options["forward_to"] = [int(target.id)]

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time pin parity"}]
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
        return CanonicalPublicationLinkedRepeatTimePinParityService().prove(
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


def test_repeat_time_pin_has_exact_read_only_composition_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                options={
                    "silent": True,
                    "autodelete_seconds": 90,
                    "autodelete_report": True,
                    "pin_on": True,
                },
            )

            proof = await _prove(Session, publication_id, task_id)
            assert proof is not None
            assert proof.repeat_seconds == 60
            assert proof.time_autodelete_seconds == 90
            assert proof.pin_on is True
            assert proof.forward_channel_ids == ()
            assert proof.views_autodelete_threshold is None
            assert proof.autodelete_report is True
            await _assert_pristine(Session, publication_id, task_id)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["pin_on"] = False
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id) is None
            await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_pin_parity_rejects_plain_forward_views_and_generated_drift(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-parity-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = (
                {"autodelete_seconds": 90},
                {"autodelete_seconds": 90, "pin_on": True, "forward_to": ["target"]},
                {"autodelete_seconds": 90, "pin_on": True, "autodelete_views": 10},
            )
            for index, options in enumerate(cases, start=2):
                publication_id, task_id = await _seed(
                    Session,
                    seed=index,
                    options=options,
                )
                assert await _prove(Session, publication_id, task_id) is None
                await _assert_pristine(Session, publication_id, task_id)

            publication_id, task_id = await _seed(
                Session,
                seed=5,
                options={"autodelete_seconds": 90, "pin_on": True},
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
