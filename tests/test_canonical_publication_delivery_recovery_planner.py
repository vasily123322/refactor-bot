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
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
)
from app.services.canonical_publication_delivery_recovery_planner import (
    CanonicalPublicationDeliveryRecoveryPlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_errors import UNKNOWN_DELIVERY_ERROR


async def _seed_claimed_publication(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    claim_at: datetime,
):
    async with Session() as session:
        owner = Client(
            tg_user_id=121000 + seed,
            username=f"canonical-recovery-{seed}",
            full_name=f"Canonical Recovery {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(121100 + seed),
            title=f"Canonical Recovery {seed}",
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
                        "text": "Canonical recovery proof",
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
        publication_id = int(publication.id)
        task_id = int(publication.legacy_post_task_id or 0)

        task = await session.get(PostTask, task_id)
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()

        claim = await CanonicalPublicationDeliveryClaimService(session).claim(
            publication_id=publication_id,
            holder="recovery-source",
            ttl_seconds=30,
            now=claim_at,
        )
        assert claim is not None
        return publication_id, claim


def test_expired_canonical_claim_plans_only_ambiguous_failure_after_transport_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-recovery-ambiguous.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim_at = scheduled_at + timedelta(minutes=1)
            publication_id, claim = await _seed_claimed_publication(
                Session,
                seed=1,
                scheduled_at=scheduled_at,
                claim_at=claim_at,
            )

            async with Session() as session:
                assert await session.get(PostTask, 1) is None
                lease_service = CanonicalPublicationDeliveryClaimService(session)
                expired = await lease_service.expired(
                    now=claim.lease.expires_at + timedelta(seconds=1)
                )
                assert len(expired) == 1
                assert expired[0].publication_id == publication_id

                result = await CanonicalPublicationDeliveryRecoveryPlanner(session).plan(
                    expired[0],
                    at=claim.lease.expires_at + timedelta(seconds=1),
                )
                assert result.outcome == "ambiguous"
                assert result.plan is not None
                assert result.plan.publication_id == publication_id
                assert result.plan.lease_token == claim.lease.lease_token
                assert result.plan.attempt == 1
                assert result.plan.error == UNKNOWN_DELIVERY_ERROR

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_late_live_heartbeat_turns_stale_expired_reference_into_not_expired(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-recovery-heartbeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim_at = scheduled_at + timedelta(minutes=1)
            publication_id, claim = await _seed_claimed_publication(
                Session,
                seed=2,
                scheduled_at=scheduled_at,
                claim_at=claim_at,
            )
            observed_at = claim.lease.expires_at + timedelta(seconds=1)

            async with Session() as session:
                service = CanonicalPublicationDeliveryClaimService(session)
                expired = await service.expired(now=observed_at)
                assert len(expired) == 1
                renewed = await service.renew(
                    claim.lease,
                    ttl_seconds=180,
                    now=observed_at,
                )
                assert renewed is not None
                assert renewed.publication_id == publication_id
                assert renewed.lease_token == claim.lease.lease_token

                result = await CanonicalPublicationDeliveryRecoveryPlanner(session).plan(
                    expired[0],
                    at=observed_at,
                )
                assert result.outcome == "not_expired"
                assert result.plan is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_takeover_invalidates_stale_expired_reference(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-recovery-takeover.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim_at = scheduled_at + timedelta(minutes=1)
            publication_id, claim = await _seed_claimed_publication(
                Session,
                seed=3,
                scheduled_at=scheduled_at,
                claim_at=claim_at,
            )
            observed_at = claim.lease.expires_at + timedelta(seconds=1)

            async with Session() as session:
                service = CanonicalPublicationDeliveryClaimService(session)
                expired = await service.expired(now=observed_at)
                assert len(expired) == 1
                takeover = await service.take_expired(
                    expired[0],
                    holder="recovery-owner",
                    now=observed_at,
                )
                assert takeover is not None
                assert takeover.publication_id == publication_id
                assert takeover.lease_token != expired[0].lease_token

                stale = await CanonicalPublicationDeliveryRecoveryPlanner(session).plan(
                    expired[0],
                    at=observed_at,
                )
                assert stale.outcome == "conflict"
                assert stale.plan is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_delivery_evidence_drift_blocks_ambiguous_recovery_plan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-recovery-evidence.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim_at = scheduled_at + timedelta(minutes=1)
            publication_id, claim = await _seed_claimed_publication(
                Session,
                seed=4,
                scheduled_at=scheduled_at,
                claim_at=claim_at,
            )
            observed_at = claim.lease.expires_at + timedelta(seconds=1)

            async with Session() as session:
                service = CanonicalPublicationDeliveryClaimService(session)
                expired = await service.expired(now=observed_at)
                assert len(expired) == 1
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                publication.telegram_message_ids = [999]
                await session.commit()

                result = await CanonicalPublicationDeliveryRecoveryPlanner(session).plan(
                    expired[0],
                    at=observed_at,
                )
                assert result.outcome == "conflict"
                assert result.plan is None
        finally:
            await engine.dispose()

    asyncio.run(run())
