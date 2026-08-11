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
from app.services import canonical_publication_delivery_atomic_handoff_claim as module
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CanonicalPublicationAtomicHandoffClaimService,
)
from app.services.canonical_publication_legacy_transport_handoff import CUTOVER_META_KEY
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_task_lease import SchedulerTaskLeaseService


async def _seed_linked(Session, *, seed: int, now: datetime) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=194000 + seed,
            username=f"atomic-handoff-{seed}",
            full_name=f"Atomic Handoff {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100194000 + seed),
            title=f"Atomic Handoff {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Atomic {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options={},
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_atomic_handoff_success_has_no_committed_transport_free_queued_window(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'atomic-handoff-success.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 17, 20, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_linked(Session, seed=1, now=now)

            async with Session() as session:
                result = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    publication_id,
                    holder="atomic-handoff-test",
                    ttl_seconds=120,
                    at=now,
                )
                assert result.outcome == "claimed"
                assert result.claim is not None
                assert result.legacy_post_task_id == task_id

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
                assert publication.legacy_post_task_id is None
                assert publication.telegram_message_ids is None
                assert await session.get(PostTask, task_id) is None

                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert lease is not None
                assert lease.lease_token == result.claim.lease.lease_token

                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "sending"
                assert dict(attempt.meta or {}).get("canonical_delivery") is True

                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert dict(publication.meta or {})[CUTOVER_META_KEY]["atomic_claim"] is True
                assert dict(schedule.meta or {})[CUTOVER_META_KEY]["atomic_claim"] is True

            # A legacy worker that selected the task before atomic transfer cannot claim
            # any transport row after the single authority commit.
            async with Session() as session:
                stale = await SchedulerTaskLeaseService(session).claim_pending(
                    task_id=task_id,
                    holder="stale-legacy-worker",
                    ttl_seconds=60,
                    now=now + timedelta(seconds=1),
                )
                assert stale is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_preclaim_rejection_rolls_back_cutover_and_restores_pending_posttask(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'atomic-handoff-rollback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 17, 20, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_linked(Session, seed=2, now=now)

            class RejectingCapabilityClaim:
                def __init__(self, session) -> None:
                    self.session = session

                async def claim_supported(self, **kwargs):
                    # Match the real capability service's pre-commit failure contract.
                    await self.session.rollback()
                    return None

            monkeypatch.setattr(
                module,
                "CanonicalPublicationDeliveryCapabilityClaimService",
                RejectingCapabilityClaim,
            )

            async with Session() as session:
                result = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    publication_id,
                    holder="rejecting-claim",
                    ttl_seconds=120,
                    at=now,
                )
                assert result.outcome == "claim_unavailable"
                assert result.claim is None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.attempt_count == 0
                assert publication.legacy_post_task_id == task_id
                assert CUTOVER_META_KEY not in dict(publication.meta or {})
                assert task is not None
                assert task.status == "pending"
                assert await session.get(PublicationDeliveryLease, publication_id) is None
                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert attempts == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_claim_still_wins_before_atomic_transfer(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'atomic-handoff-legacy-wins.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 17, 20, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_linked(Session, seed=3, now=now)

            async with Session() as session:
                legacy = await SchedulerTaskLeaseService(session).claim_pending(
                    task_id=task_id,
                    holder="legacy-first",
                    ttl_seconds=120,
                    now=now,
                )
                assert legacy is not None

            async with Session() as session:
                result = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    publication_id,
                    holder="canonical-late",
                    ttl_seconds=120,
                    at=now + timedelta(seconds=1),
                )
                assert result.outcome == "contention"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == task_id
                assert task is not None and task.status == "processing"
                assert await session.get(PublicationDeliveryLease, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
