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
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CUTOVER_META_KEY,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.canonical_publication_repeat_delivery_executor import (
    CanonicalPublicationRepeatDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed(Session, *, seed: int) -> dict[str, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=207000 + seed,
            username=f"linked-repeat-{seed}",
            full_name=f"Linked Repeat {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100207000 + seed),
            title=f"Linked Repeat {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Linked repeat {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"silent": True},
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
        }


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        assert kwargs.get("disable_notification") is True
        return [6301]


async def _assert_original_linked(Session, seeded: dict[str, int]) -> None:
    async with Session() as session:
        publication = await session.get(Publication, seeded["publication_id"])
        task = await session.get(PostTask, seeded["task_id"])
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert publication.legacy_post_task_id == seeded["task_id"]
        assert CUTOVER_META_KEY not in dict(publication.meta or {})
        assert task is not None and task.status == "pending"
        assert await session.get(PublicationDeliveryLease, seeded["publication_id"]) is None


def test_linked_repeat_requires_continuation_availability_and_old_direct_handoff_stays_blocked(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-repeat-disabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1)

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    seeded["publication_id"],
                    holder="repeat-disabled",
                    ttl_seconds=180,
                    allow_repeat=False,
                )
                assert result.outcome == "ineligible"
            await _assert_original_linked(Session, seeded)

            async with Session() as session:
                direct = await CanonicalPublicationLegacyTransportHandoffService(
                    session
                ).retire_for_canonical_delivery(seeded["publication_id"])
                assert direct.outcome == "ineligible"
            await _assert_original_linked(Session, seeded)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_repeat_atomic_claim_publish_and_continuation_create_one_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-repeat-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2)
            claim_at = datetime.now(timezone.utc)
            async with Session() as session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    seeded["publication_id"],
                    holder="repeat-enabled",
                    ttl_seconds=180,
                    at=claim_at,
                    allow_repeat=True,
                )
                assert transfer.outcome == "claimed"
                assert transfer.claim is not None
                claim = transfer.claim

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, seeded["task_id"]) is None
                marker = dict(publication.meta or {})[CUTOVER_META_KEY]
                assert marker["repeat"] is True
                assert marker["repeat_seconds"] == 60
                assert marker["repeat_group_id"] == seeded["task_id"]
                assert marker["repeat_root"] is True
                assert await session.get(PublicationDeliveryLease, seeded["publication_id"]) is not None

            sender = _Sender()
            executor = CanonicalPublicationRepeatDeliveryExecutor(
                Session,
                sender=sender,
                allow_repeat=True,
                heartbeat_interval_seconds=120,
            )
            published = await executor.execute_claim(claim)
            assert published.outcome == "published"
            assert sender.calls == 1

            continuation = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            tick = await continuation.run_once(now=claim_at + timedelta(seconds=5))
            assert tick.materialized == 1
            assert tick.conflicts == 0

            async with Session() as session:
                source = await session.get(Publication, seeded["publication_id"])
                assert source is not None
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == seeded["publication_id"],
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "published"
                assert dict(attempt.meta or {}).get("canonical_delivery") is True
                successors = (
                    await session.execute(
                        select(Publication).where(
                            Publication.id != seeded["publication_id"],
                            Publication.content_item_id == int(source.content_item_id),
                            Publication.content_revision == int(source.content_revision),
                            Publication.channel_id == int(source.channel_id),
                        )
                    )
                ).scalars().all()
                assert len(successors) == 1
                successor = successors[0]
                assert successor.status == "queued"
                assert successor.legacy_post_task_id is not None
                successor_task = await session.get(PostTask, int(successor.legacy_post_task_id))
                assert successor_task is not None and successor_task.status == "pending"

            replay = await executor.execute(seeded["publication_id"])
            assert replay.outcome == "ineligible"
            assert sender.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_repeat_cadence_drift_rolls_back_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-repeat-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=3)
            async with Session() as session:
                task = await session.get(PostTask, seeded["task_id"])
                assert task is not None
                payload = dict(task.payload or {})
                payload["repeat_seconds"] = 61
                task.payload = payload
                await session.commit()

            async with Session() as session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    seeded["publication_id"],
                    holder="repeat-drift",
                    ttl_seconds=180,
                    allow_repeat=True,
                )
                assert transfer.outcome == "ineligible"
            await _assert_original_linked(Session, seeded)
        finally:
            await engine.dispose()

    asyncio.run(run())
