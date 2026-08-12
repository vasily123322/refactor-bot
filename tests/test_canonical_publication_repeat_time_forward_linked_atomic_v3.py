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
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
)
from app.services.canonical_publication_legacy_transport_handoff import CUTOVER_META_KEY
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session, *, seed: int) -> tuple[int, int, tuple[dict[str, int], ...]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=251000 + seed,
            username=f"repeat-time-forward-linked-{seed}",
            full_name=f"Repeat Time Forward Linked {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100251000 + seed),
            title=f"Repeat Time Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100351000 + seed),
            title=f"Repeat Time Forward A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100451000 + seed),
            title=f"Repeat Time Forward B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time forward linked"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "forward_to": [int(target_a.id), int(target_b.id)],
                "autodelete_seconds": 90,
            },
        )
        assert publication.legacy_post_task_id is not None
        return (
            int(publication.id),
            int(publication.legacy_post_task_id),
            (
                {"channel_id": int(target_a.id), "telegram_chat_id": int(target_a.tg_chat_id)},
                {"channel_id": int(target_b.id), "telegram_chat_id": int(target_b.tg_chat_id)},
            ),
        )


async def _handoff(
    Session,
    publication_id: int,
    *,
    allow_time_autodelete: bool = True,
    allow_repeat_time: bool = True,
    allow_repeat_time_forward: bool = True,
):
    async with Session() as session:
        return await CanonicalPublicationLinkedRepeatAtomicHandoffService(
            session
        ).claim_linked_repeat(
            publication_id,
            holder="repeat-time-forward-linked",
            ttl_seconds=180,
            allow_repeat=True,
            allow_time_autodelete=allow_time_autodelete,
            allow_repeat_time=allow_repeat_time,
            allow_repeat_time_forward=allow_repeat_time_forward,
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
        assert CUTOVER_META_KEY not in dict(publication.meta or {})
        assert task is not None and task.status == "pending"
        assert attempts == []
        assert await session.get(PublicationDeliveryLease, publication_id) is None


def test_linked_repeat_time_forward_cutover_snapshots_ordered_targets_atomically(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-linked.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, targets = await _seed(Session, seed=1)

            result = await _handoff(Session, publication_id)
            assert result.outcome == "claimed"
            assert result.claim is not None
            assert result.legacy_post_task_id == task_id
            assert tuple(result.claim.plan.runtime_options()["forward_to"]) == tuple(
                target["channel_id"] for target in targets
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                lease = await session.get(PublicationDeliveryLease, publication_id)
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
                assert task is None
                assert lease is not None
                assert str(lease.lease_token) == str(result.claim.lease.lease_token)
                assert attempt.status == "sending"
                assert dict(attempt.meta or {})[FORWARD_TARGET_SNAPSHOT_META_KEY] == list(targets)
                assert CUTOVER_META_KEY in dict(publication.meta or {})
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_time_forward_fact_rolls_prepared_cutover_back_completely(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-linked-rollback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = (
                (2, False, True, True),
                (3, True, False, True),
                (4, True, True, False),
            )
            for seed, allow_time, allow_repeat_time, allow_time_forward in cases:
                publication_id, task_id, _ = await _seed(Session, seed=seed)
                result = await _handoff(
                    Session,
                    publication_id,
                    allow_time_autodelete=allow_time,
                    allow_repeat_time=allow_repeat_time,
                    allow_repeat_time_forward=allow_time_forward,
                )
                assert result.outcome == "claim_unavailable"
                assert result.claim is None
                assert result.legacy_post_task_id == task_id
                await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_forward_order_drift_fails_before_authority_transition(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-linked-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(Session, seed=5)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["forward_to"] = list(reversed(payload["forward_to"]))
                task.payload = payload
                await session.commit()

            result = await _handoff(Session, publication_id)
            assert result.outcome == "ineligible"
            assert result.claim is None
            await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
