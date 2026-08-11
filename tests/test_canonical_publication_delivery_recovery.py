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
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_recovery import (
    CanonicalPublicationDeliveryRecoveryService,
)
from app.services.scheduler_errors import UNKNOWN_DELIVERY_ERROR


async def _seed_sending(
    Session,
    *,
    seed: int,
    expires_at: datetime,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=128000 + seed,
            username=f"canonical-delivery-recovery-{seed}",
            full_name=f"Canonical Delivery Recovery {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(128100 + seed),
            title=f"Canonical Delivery Recovery {seed}",
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
                        "text": "Canonical delivery recovery proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=int(item.current_revision),
            channel_id=int(channel.id),
            scheduled_at=expires_at - timedelta(minutes=5),
            timezone="UTC",
            status="pending",
            repeat_rule={},
            meta={},
        )
        session.add(schedule)
        await session.flush()
        publication = Publication(
            schedule_entry_id=int(schedule.id),
            content_item_id=int(item.id),
            content_revision=int(item.current_revision),
            channel_id=int(channel.id),
            status="sending",
            legacy_post_task_id=None,
            telegram_message_ids=None,
            result_link=None,
            last_error=None,
            attempt_count=1,
            meta={},
        )
        session.add(publication)
        await session.flush()
        publication_id = int(publication.id)
        session.add_all(
            [
                PublicationAttempt(
                    publication_id=publication_id,
                    attempt=1,
                    status="sending",
                    telegram_message_ids=None,
                    error=None,
                    meta={"canonical_delivery": True},
                    finished_at=None,
                ),
                PublicationDeliveryLease(
                    publication_id=publication_id,
                    lease_token=f"expired-token-{seed}",
                    holder="delivery-worker",
                    expires_at=expires_at,
                ),
            ]
        )
        await session.commit()
        return publication_id


def test_recovery_batch_fails_expired_ambiguous_delivery_without_post_task(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-recovery.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            expired_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_sending(
                Session,
                seed=1,
                expires_at=expired_at,
            )

            tick = await CanonicalPublicationDeliveryRecoveryService(Session).run_once(
                now=expired_at + timedelta(seconds=1),
            )
            assert tick.selected == 1
            assert tick.taken_over == 1
            assert tick.failed_unknown == 1
            assert tick.contention == 0
            assert tick.conflicts == 0
            assert tick.failures == 0

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "failed"
                assert publication.last_error == UNKNOWN_DELIVERY_ERROR
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None and schedule.status == "failed"
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                assert attempt.status == "failed"
                assert attempt.error == UNKNOWN_DELIVERY_ERROR
                assert attempt.finished_at is not None
                assert await session.get(PublicationDeliveryLease, publication_id) is None
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_live_delivery_lease_is_not_selected_for_recovery(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-recovery-live.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_sending(
                Session,
                seed=2,
                expires_at=now + timedelta(minutes=1),
            )

            tick = await CanonicalPublicationDeliveryRecoveryService(Session).run_once(
                now=now,
            )
            assert tick.selected == 0
            assert tick.taken_over == 0

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None and publication.status == "sending"
                assert await session.get(PublicationDeliveryLease, publication_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_delivery_worker_cannot_revive_before_recovery_takeover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-recovery-expired-renew.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            expired_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_sending(
                Session,
                seed=3,
                expires_at=expired_at,
            )
            recovery_now = expired_at + timedelta(seconds=1)

            async with Session() as session:
                renewed = await CanonicalPublicationDeliveryClaimService(session).renew(
                    CanonicalPublicationDeliveryLeaseHandle(
                        publication_id=publication_id,
                        lease_token="expired-token-3",
                        holder="delivery-worker",
                        expires_at=expired_at,
                    ),
                    ttl_seconds=180,
                    now=recovery_now,
                )
                assert renewed is None
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert lease is not None
                assert lease.lease_token == "expired-token-3"

            tick = await CanonicalPublicationDeliveryRecoveryService(Session).run_once(
                now=recovery_now,
            )
            assert tick.selected == 1
            assert tick.taken_over == 1
            assert tick.failed_unknown == 1
            assert tick.contention == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_recovery_takeover_counts_contention(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-recovery-race.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            expired_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_sending(
                Session,
                seed=4,
                expires_at=expired_at,
            )
            recovery_now = expired_at + timedelta(seconds=1)

            class _OtherRecoveryFirst(CanonicalPublicationDeliveryRecoveryService):
                async def _recover_reference(self, reference, *, now):
                    async with Session() as session:
                        takeover = await CanonicalPublicationDeliveryClaimService(
                            session
                        ).take_expired(
                            reference,
                            holder="other-recovery-worker",
                            now=now,
                        )
                        assert takeover is not None
                    return await super()._recover_reference(reference, now=now)

            tick = await _OtherRecoveryFirst(Session).run_once(now=recovery_now)
            assert tick.selected == 1
            assert tick.taken_over == 0
            assert tick.contention == 1
            assert tick.failed_unknown == 0

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None and publication.status == "sending"
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert lease is not None
                assert lease.lease_token != "expired-token-4"
                assert lease.holder == "other-recovery-worker"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_takeover_lifecycle_conflict_keeps_recovery_lease_as_barrier(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-recovery-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            expired_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_sending(
                Session,
                seed=5,
                expires_at=expired_at,
            )
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                publication.telegram_message_ids = [1801]
                await session.commit()

            tick = await CanonicalPublicationDeliveryRecoveryService(Session).run_once(
                now=expired_at + timedelta(seconds=1),
            )
            assert tick.selected == 1
            assert tick.taken_over == 1
            assert tick.failed_unknown == 0
            assert tick.conflicts == 1

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.telegram_message_ids == [1801]
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert lease is not None
                assert lease.lease_token != "expired-token-5"
                assert lease.holder == "canonical-publication-delivery-recovery"
        finally:
            await engine.dispose()

    asyncio.run(run())
