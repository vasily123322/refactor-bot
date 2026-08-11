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


async def _seed_root(Session, *, seed: int, runtime_options: dict | None = None):
    async with Session() as session:
        owner = Client(
            tg_user_id=206000 + seed,
            username=f"repeat-parity-{seed}",
            full_name=f"Repeat Parity {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100206000 + seed),
            title=f"Repeat Parity {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Repeat parity {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=runtime_options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        plan = await CanonicalPublicationDeliveryPlanner(session).plan(
            int(publication.id),
            at=datetime.now(timezone.utc),
        )
        assert plan is not None
        return int(publication.id), int(task.id), plan


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


def test_bridge_root_repeat_proves_fixed_delay_lineage_without_payload_group_id(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-root-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, plan = await _seed_root(
                Session,
                seed=1,
                runtime_options={"silent": True},
            )
            proof = await _prove(Session, publication_id, task_id, plan)
            assert proof is not None
            assert proof.publication_id == publication_id
            assert proof.legacy_post_task_id == task_id
            assert proof.repeat_group_id == task_id
            assert proof.repeat_seconds == 60
            assert proof.root_occurrence is True
            assert proof.pin_on is False
            assert proof.forward_channel_ids == ()
            assert proof.views_autodelete_threshold is None
            assert proof.autodelete_report is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_explicit_successor_group_id_must_match_canonical_lineage(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-successor-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, plan = await _seed_root(Session, seed=2)
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                group_id = int(dict(publication.meta or {})["repeat_group_id"])
                payload = dict(task.payload or {})
                payload["repeat_group_id"] = group_id
                task.payload = payload
                await session.commit()

            proof = await _prove(Session, publication_id, task_id, plan)
            assert proof is not None
            assert proof.root_occurrence is False
            assert proof.repeat_group_id == task_id

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["repeat_group_id"] = task_id + 100
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_seconds_or_canonical_group_drift_blocks_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-drift-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, plan = await _seed_root(Session, seed=3)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["repeat_seconds"] = 61
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id, plan) is None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                payload = dict(task.payload or {})
                payload["repeat_seconds"] = 60
                task.payload = payload
                publication.meta = {
                    **dict(publication.meta or {}),
                    "repeat_group_id": task_id + 1,
                }
                await session.commit()
            assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_pin_parity_requires_exact_legacy_pin_intent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-pin-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, plan = await _seed_root(
                Session,
                seed=4,
                runtime_options={"silent": True, "pin_on": True},
            )
            proof = await _prove(Session, publication_id, task_id, plan)
            assert proof is not None
            assert proof.pin_on is True
            assert proof.repeat_seconds == 60
            assert proof.forward_channel_ids == ()
            assert proof.views_autodelete_threshold is None

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


def test_repeat_views_parity_requires_exact_threshold(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, plan = await _seed_root(
                Session,
                seed=5,
                runtime_options={"silent": True, "autodelete_views": 17},
            )

            proof = await _prove(Session, publication_id, task_id, plan)
            assert proof is not None
            assert proof.views_autodelete_threshold == 17
            assert proof.autodelete_report is False
            assert proof.pin_on is False
            assert proof.forward_channel_ids == ()

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_views"] = 18
                task.payload = payload
                await session.commit()

            assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_report_requires_exact_legacy_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-report-parity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, plan = await _seed_root(
                Session,
                seed=6,
                runtime_options={"autodelete_views": 9, "autodelete_report": True},
            )

            proof = await _prove(Session, publication_id, task_id, plan)
            assert proof is not None
            assert proof.views_autodelete_threshold == 9
            assert proof.autodelete_report is True

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_report"] = False
                task.payload = payload
                await session.commit()

            assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_parity_keeps_compositions_and_time_outside_stage(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            cases = [
                (7, {"autodelete_seconds": 60}),
                (8, {"autodelete_seconds": 60, "autodelete_views": 5}),
                (9, {"pin_on": True, "autodelete_views": 5}),
                (10, {"forward_to": [999], "autodelete_views": 5}),
            ]
            for seed, options in cases:
                publication_id, task_id, plan = await _seed_root(
                    Session,
                    seed=seed,
                    runtime_options=options,
                )
                assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_generated_execution_evidence_blocks_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-generated-state.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, plan = await _seed_root(
                Session,
                seed=11,
                runtime_options={"autodelete_views": 12},
            )
            assert await _prove(Session, publication_id, task_id, plan) is not None

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_at"] = datetime.now(timezone.utc).isoformat()
                task.payload = payload
                await session.commit()

            assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_generated_result_evidence_blocks_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-result-evidence.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, plan = await _seed_root(Session, seed=12)
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["result_ids"] = [7777]
                task.payload = payload
                await session.commit()
            assert await _prove(Session, publication_id, task_id, plan) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
