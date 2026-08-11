from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_linked_forward_parity import (
    CanonicalPublicationLinkedForwardParityService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_linked(
    Session,
    *,
    seed: int,
    runtime_builder,
):
    async with Session() as session:
        owner = Client(
            tg_user_id=198000 + seed,
            username=f"forward-parity-{seed}",
            full_name=f"Forward Parity {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100198000 + seed),
            title=f"Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100198100 + seed),
            title=f"Forward A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100198200 + seed),
            title=f"Forward B {seed}",
            owner_id=int(owner.id),
            is_active=False,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()

        runtime_options = runtime_builder(target_a, target_b)
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Forward parity {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=runtime_options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        plan = await CanonicalPublicationDeliveryPlanner(session).plan(
            int(publication.id),
            at=datetime.now(timezone.utc),
        )
        assert plan is not None
        return (
            int(publication.id),
            int(task.id),
            int(source.tg_chat_id),
            int(target_a.id),
            int(target_a.tg_chat_id),
            int(target_b.id),
            int(target_b.tg_chat_id),
            plan,
        )


def test_exact_ordered_forward_parity_resolves_internal_channel_ids(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-parity-exact.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            (
                publication_id,
                task_id,
                source_chat,
                target_a_id,
                target_a_chat,
                target_b_id,
                target_b_chat,
                plan,
            ) = await _seed_linked(
                Session,
                seed=1,
                runtime_builder=lambda a, b: {
                    "silent": True,
                    "forward_to": [int(b.id), int(a.id)],
                },
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                proof = await CanonicalPublicationLinkedForwardParityService(
                    session
                ).prove(
                    task=task,
                    publication=publication,
                    plan=plan,
                )
                assert proof is not None
                assert proof.source_telegram_chat_id == source_chat
                assert proof.forward_channel_ids == (target_b_id, target_a_id)
                assert [target.telegram_chat_id for target in proof.forward_targets] == [
                    target_b_chat,
                    target_a_chat,
                ]
                assert proof.disable_notification is True
                assert proof.pin_on is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_target_order_drift_blocks_forward_parity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-parity-order-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, *_rest, plan = await _seed_linked(
                Session,
                seed=2,
                runtime_builder=lambda a, b: {
                    "forward_to": [int(a.id), int(b.id)]
                },
            )
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["forward_to"] = list(reversed(payload["forward_to"]))
                task.payload = payload
                await session.commit()

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                assert (
                    await CanonicalPublicationLinkedForwardParityService(session).prove(
                        task=task,
                        publication=publication,
                        plan=plan,
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_forward_target_is_clean_parity_rejection(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-parity-missing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, *_rest, plan = await _seed_linked(
                Session,
                seed=3,
                runtime_builder=lambda a, b: {"forward_to": [int(a.id)]},
            )
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                target_id = int(dict(task.payload or {})["forward_to"][0])
                target = await session.get(Channel, target_id)
                assert target is not None
                await session.delete(target)
                await session.commit()

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                assert (
                    await CanonicalPublicationLinkedForwardParityService(session).prove(
                        task=task,
                        publication=publication,
                        plan=plan,
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_forward_parity_allows_exact_pin_but_rejects_timer_and_execution_evidence(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-parity-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            publication_id, task_id, *_rest, pin_plan = await _seed_linked(
                Session,
                seed=4,
                runtime_builder=lambda a, b: {
                    "forward_to": [int(a.id)],
                    "pin_on": True,
                },
            )
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                proof = await CanonicalPublicationLinkedForwardParityService(session).prove(
                    task=task,
                    publication=publication,
                    plan=pin_plan,
                )
                assert proof is not None
                assert proof.pin_on is True

            publication_id, task_id, *_rest, timer_plan = await _seed_linked(
                Session,
                seed=5,
                runtime_builder=lambda a, b: {
                    "forward_to": [int(a.id)],
                    "autodelete_seconds": 60,
                },
            )
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                assert (
                    await CanonicalPublicationLinkedForwardParityService(session).prove(
                        task=task,
                        publication=publication,
                        plan=timer_plan,
                    )
                    is None
                )

            publication_id, task_id, *_rest, evidence_plan = await _seed_linked(
                Session,
                seed=6,
                runtime_builder=lambda a, b: {"forward_to": [int(a.id)]},
            )
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["result_ids"] = [6001]
                task.payload = payload
                await session.commit()
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                assert (
                    await CanonicalPublicationLinkedForwardParityService(session).prove(
                        task=task,
                        publication=publication,
                        plan=evidence_plan,
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
