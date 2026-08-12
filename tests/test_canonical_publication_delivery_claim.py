from __future__ import annotations

import asyncio
from copy import deepcopy
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
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
)
from app.services.publication_bridge import LegacyPublicationBridge


_RUNTIME_OPTIONS = {
    "silent": True,
    "pin_on": False,
    "autodelete_seconds": 3600,
    "nested": {"mode": "stable"},
}


async def _seed_queued_publication(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=118000 + seed,
            username=f"canonical-delivery-claim-{seed}",
            full_name=f"Canonical Delivery Claim {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(118100 + seed),
            title=f"Canonical Delivery Claim {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Canonical claimed delivery",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        return (
            int(publication.id),
            int(publication.legacy_post_task_id or 0),
            int(channel.id),
        )


def test_claim_is_transport_independent_and_creates_one_attempt_and_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-claim.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id, channel_id = await _seed_queued_publication(
                Session,
                seed=1,
                scheduled_at=scheduled_at,
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                now = scheduled_at + timedelta(minutes=5)
                service = CanonicalPublicationDeliveryClaimService(session)
                claim = await service.claim(
                    publication_id=publication_id,
                    holder="claim-test",
                    ttl_seconds=180,
                    now=now,
                )
                assert claim is not None
                assert claim.attempt == 1
                assert claim.plan.publication_id == publication_id
                assert claim.plan.channel_id == channel_id
                assert claim.plan.runtime_options() == _RUNTIME_OPTIONS
                assert claim.plan.post_document().blocks[0]["text"] == (
                    "Canonical claimed delivery"
                )

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
                assert publication.legacy_post_task_id is None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert schedule.status == "pending"

                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert len(attempts) == 1
                attempt = attempts[0]
                assert attempt.attempt == 1
                assert attempt.status == "sending"
                assert attempt.finished_at is None
                assert attempt.telegram_message_ids is None
                assert attempt.error is None
                assert attempt.meta == {"canonical_delivery": True}

                lease = await service.current(publication_id)
                assert lease is not None
                assert lease.lease_token == claim.lease.lease_token
                assert lease.holder == "claim-test"

                assert await service.claim(
                    publication_id=publication_id,
                    holder="duplicate",
                    now=now,
                ) is None
                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert len(attempts) == 1

                renewed = await service.renew(
                    claim.lease,
                    ttl_seconds=240,
                    now=now + timedelta(seconds=30),
                )
                assert renewed is not None
                assert renewed.lease_token == claim.lease.lease_token
                assert renewed.expires_at == now + timedelta(seconds=270)

                expired = await service.expired(
                    now=renewed.expires_at + timedelta(seconds=1)
                )
                assert len(expired) == 1
                assert expired[0].publication_id == publication_id
                recovery = await service.take_expired(
                    expired[0],
                    holder="recovery-test",
                    now=renewed.expires_at + timedelta(seconds=1),
                )
                assert recovery is not None
                assert recovery.publication_id == publication_id
                assert recovery.lease_token != renewed.lease_token
                assert recovery.holder == "recovery-test"
                assert await service.renew(
                    renewed,
                    now=renewed.expires_at + timedelta(seconds=2),
                ) is None

                # A recovery-owned lease still protects an ambiguous ``sending``
                # delivery. It cannot be removed independently of terminal resolution.
                assert await service.release(recovery) is False
                current = await service.current(publication_id)
                assert current is not None
                assert current.lease_token == recovery.lease_token
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_even_expired_lease_is_recovery_barrier_and_claim_rolls_back(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-lease-barrier.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _task_id, _channel_id = await _seed_queued_publication(
                Session,
                seed=2,
                scheduled_at=scheduled_at,
            )

            async with Session() as session:
                session.add(
                    PublicationDeliveryLease(
                        publication_id=publication_id,
                        lease_token="expired-recovery-barrier",
                        holder="old-worker",
                        expires_at=scheduled_at - timedelta(minutes=1),
                    )
                )
                await session.commit()

                service = CanonicalPublicationDeliveryClaimService(session)
                assert await service.claim(
                    publication_id=publication_id,
                    holder="new-worker",
                    now=scheduled_at + timedelta(minutes=1),
                ) is None

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.attempt_count == 0
                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert attempts == []
                lease = await service.current(publication_id)
                assert lease is not None
                assert lease.lease_token == "expired-recovery-barrier"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_future_or_inactive_publication_cannot_be_claimed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-claim-eligibility.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _task_id, channel_id = await _seed_queued_publication(
                Session,
                seed=3,
                scheduled_at=scheduled_at,
            )

            async with Session() as session:
                service = CanonicalPublicationDeliveryClaimService(session)
                assert await service.claim(
                    publication_id=publication_id,
                    holder="too-early",
                    now=scheduled_at - timedelta(seconds=1),
                ) is None

                channel = await session.get(Channel, channel_id)
                assert channel is not None
                channel.is_active = False
                await session.commit()
                assert await service.claim(
                    publication_id=publication_id,
                    holder="inactive",
                    now=scheduled_at + timedelta(minutes=1),
                ) is None

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.attempt_count == 0
                assert await service.current(publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
