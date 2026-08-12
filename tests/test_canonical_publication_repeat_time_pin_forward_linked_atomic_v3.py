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


async def _seed(Session, seed: int):
    async with Session() as session:
        owner = Client(
            tg_user_id=257000 + seed,
            username=f"combined-linked-{seed}",
            full_name="Combined Linked",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100257000 + seed), title="Source", owner_id=int(owner.id), is_active=True
        )
        a = Channel(
            tg_chat_id=-(100357000 + seed), title="A", owner_id=int(owner.id), is_active=True
        )
        b = Channel(
            tg_chat_id=-(100457000 + seed), title="B", owner_id=int(owner.id), is_active=True
        )
        session.add_all([source, a, b])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(blocks=[{"id": "b1", "type": "text", "text": "combined"}]),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "pin_on": True,
                "forward_to": [int(a.id), int(b.id)],
                "autodelete_seconds": 90,
            },
        )
        assert publication.legacy_post_task_id is not None
        return (
            int(publication.id),
            int(publication.legacy_post_task_id),
            [
                {"channel_id": int(a.id), "telegram_chat_id": int(a.tg_chat_id)},
                {"channel_id": int(b.id), "telegram_chat_id": int(b.tg_chat_id)},
            ],
        )


async def _handoff(Session, publication_id: int, *, allow_combined: bool = True):
    async with Session() as session:
        return await CanonicalPublicationLinkedRepeatAtomicHandoffService(
            session
        ).claim_linked_repeat(
            publication_id,
            holder="combined-linked",
            ttl_seconds=180,
            allow_repeat=True,
            allow_time_autodelete=True,
            allow_repeat_time=True,
            allow_repeat_time_pin=True,
            allow_repeat_time_forward=True,
            allow_repeat_time_pin_forward=allow_combined,
        )


async def _assert_pristine(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        assert publication is not None
        assert publication.status == "queued"
        assert publication.legacy_post_task_id == task_id
        assert CUTOVER_META_KEY not in dict(publication.meta or {})
        assert task is not None and task.status == "pending"
        assert await session.get(PublicationDeliveryLease, publication_id) is None
        assert (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id
                )
            )
        ).scalars().all() == []


def test_combined_linked_handoff_claims_and_snapshots_targets_atomically(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-linked.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, targets = await _seed(Session, 1)
            result = await _handoff(Session, publication_id)
            assert result.outcome == "claimed"
            assert result.claim is not None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
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
                assert await session.get(PostTask, task_id) is None
                assert attempt.status == "sending"
                assert dict(attempt.meta or {})[FORWARD_TARGET_SNAPSHOT_META_KEY] == targets
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_both_narrower_facts_without_combined_fact_roll_back_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-linked-rollback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(Session, 2)
            result = await _handoff(Session, publication_id, allow_combined=False)
            assert result.outcome == "claim_unavailable"
            assert result.claim is None
            await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_combined_target_order_drift_fails_before_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-linked-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(Session, 3)
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["forward_to"] = list(reversed(payload["forward_to"]))
                task.payload = payload
                await session.commit()
            result = await _handoff(Session, publication_id)
            assert result.outcome == "ineligible"
            await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
