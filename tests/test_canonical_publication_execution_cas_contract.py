from __future__ import annotations

import asyncio
from dataclasses import replace
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
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
    CanonicalPublicationDeliveryClaimService,
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_errors import UNKNOWN_DELIVERY_ERROR


async def _seed_transport_retired_publication(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=190000 + seed,
            username=f"canonical-cas-{seed}",
            full_name=f"Canonical CAS {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(190100 + seed),
            title=f"Canonical CAS {seed}",
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
                        "text": "Canonical execution generation proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options={},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


async def _attempts(session, publication_id: int) -> list[PublicationAttempt]:
    return list(
        (
            await session.execute(
                select(PublicationAttempt)
                .where(PublicationAttempt.publication_id == int(publication_id))
                .order_by(PublicationAttempt.attempt.asc())
            )
        ).scalars().all()
    )


class _RecordingSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        self.calls += 1
        return [99001]


def test_two_claimers_have_one_winner_and_one_durable_generation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-cas-race.db'}",
            connect_args={"timeout": 30},
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_transport_retired_publication(
                Session,
                seed=1,
                scheduled_at=now - timedelta(minutes=1),
            )

            async def claim(holder: str):
                async with Session() as session:
                    return await CanonicalPublicationDeliveryClaimService(session).claim(
                        publication_id=publication_id,
                        holder=holder,
                        now=now,
                    )

            first, second = await asyncio.gather(claim("claimer-a"), claim("claimer-b"))
            winners = [claim for claim in (first, second) if claim is not None]
            assert len(winners) == 1
            winner = winners[0]
            assert winner.attempt == 1
            assert winner.plan.publication_id == publication_id
            assert winner.lease.publication_id == publication_id

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
                attempts = await _attempts(session, publication_id)
                assert [(attempt.attempt, attempt.status) for attempt in attempts] == [
                    (1, "sending")
                ]
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert lease is not None
                assert lease.lease_token == winner.lease.lease_token
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_generation_is_rejected_before_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-cas-stale-generation.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_transport_retired_publication(
                Session,
                seed=2,
                scheduled_at=now - timedelta(minutes=1),
            )
            async with Session() as session:
                claim = await CanonicalPublicationDeliveryClaimService(session).claim(
                    publication_id=publication_id,
                    holder="generation-owner",
                    now=now,
                )
            assert claim is not None

            stale = CanonicalPublicationDeliveryClaim(
                plan=claim.plan,
                lease=claim.lease,
                attempt=2,
            )
            sender = _RecordingSender()
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
            ).execute_claim(stale)
            assert result.outcome == "ineligible"
            assert sender.calls == 0

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
                attempts = await _attempts(session, publication_id)
                assert [attempt.attempt for attempt in attempts] == [1]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_token_cannot_renew_or_finalize_current_generation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-cas-stale-token.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_transport_retired_publication(
                Session,
                seed=3,
                scheduled_at=now - timedelta(minutes=1),
            )
            async with Session() as session:
                claim = await CanonicalPublicationDeliveryClaimService(session).claim(
                    publication_id=publication_id,
                    holder="token-owner",
                    now=now,
                )
            assert claim is not None
            stale = CanonicalPublicationDeliveryLeaseHandle(
                publication_id=publication_id,
                lease_token="stale-token",
                holder="stale-owner",
                expires_at=claim.lease.expires_at,
            )

            async with Session() as session:
                renewed = await CanonicalPublicationDeliveryClaimService(session).renew(
                    stale,
                    now=now + timedelta(seconds=1),
                )
                assert renewed is None
            async with Session() as session:
                finalized = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_failure(
                    stale,
                    error="must-not-write",
                    now=now + timedelta(seconds=1),
                )
                assert finalized.outcome == "conflict"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.last_error is None
                attempts = await _attempts(session, publication_id)
                assert len(attempts) == 1
                assert attempts[0].status == "sending"
                assert attempts[0].finished_at is None
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert lease is not None
                assert lease.lease_token == claim.lease.lease_token
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_already_claimed_cancelled_and_terminal_publications_do_not_reclaim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-cas-terminal.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            claimed_id = await _seed_transport_retired_publication(
                Session,
                seed=4,
                scheduled_at=now - timedelta(minutes=1),
            )
            cancelled_id = await _seed_transport_retired_publication(
                Session,
                seed=5,
                scheduled_at=now - timedelta(minutes=1),
            )
            terminal_id = await _seed_transport_retired_publication(
                Session,
                seed=6,
                scheduled_at=now - timedelta(minutes=1),
            )

            async with Session() as session:
                first = await CanonicalPublicationDeliveryClaimService(session).claim(
                    publication_id=claimed_id,
                    holder="first-owner",
                    now=now,
                )
                assert first is not None
                cancelled = await session.get(Publication, cancelled_id)
                terminal = await session.get(Publication, terminal_id)
                assert cancelled is not None and terminal is not None
                cancelled.status = "cancelled"
                terminal.status = "published"
                await session.commit()

            for publication_id in (claimed_id, cancelled_id, terminal_id):
                async with Session() as session:
                    assert await CanonicalPublicationDeliveryClaimService(session).claim(
                        publication_id=publication_id,
                        holder="second-owner",
                        now=now,
                    ) is None

            async with Session() as session:
                assert len(await _attempts(session, claimed_id)) == 1
                assert await _attempts(session, cancelled_id) == []
                assert await _attempts(session, terminal_id) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expiry_recovery_closes_same_generation_without_creating_retry(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-cas-recovery-generation.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_transport_retired_publication(
                Session,
                seed=7,
                scheduled_at=now - timedelta(minutes=1),
            )
            async with Session() as session:
                claim = await CanonicalPublicationDeliveryClaimService(session).claim(
                    publication_id=publication_id,
                    holder="delivery-owner",
                    ttl_seconds=30,
                    now=now,
                )
            assert claim is not None
            recovery_now = claim.lease.expires_at + timedelta(seconds=1)

            async with Session() as session:
                service = CanonicalPublicationDeliveryClaimService(session)
                refs = await service.expired(now=recovery_now)
                assert len(refs) == 1
                recovery = await service.take_expired(
                    refs[0],
                    holder="recovery-owner",
                    now=recovery_now,
                )
                assert recovery is not None
                assert recovery.lease_token != claim.lease.lease_token

            async with Session() as session:
                result = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_failure(
                    recovery,
                    error=UNKNOWN_DELIVERY_ERROR,
                    now=recovery_now,
                    finished_at=recovery_now,
                )
                assert result.outcome == "failed"
                assert result.attempt == 1

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "failed"
                assert publication.attempt_count == 1
                attempts = await _attempts(session, publication_id)
                assert [(attempt.attempt, attempt.status) for attempt in attempts] == [
                    (1, "failed")
                ]
                assert await session.get(PublicationDeliveryLease, publication_id) is None

                # Ambiguous recovery is terminal for this occurrence; it must not mint
                # generation 2 or re-authorize a primary provider call.
                assert await CanonicalPublicationDeliveryClaimService(session).claim(
                    publication_id=publication_id,
                    holder="retry-owner",
                    now=recovery_now + timedelta(seconds=1),
                ) is None
                assert [attempt.attempt for attempt in await _attempts(session, publication_id)] == [1]
        finally:
            await engine.dispose()

    asyncio.run(run())
