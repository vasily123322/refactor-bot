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
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimRequirements,
    CanonicalPublicationDeliveryClaimService,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CUTOVER_META_KEY,
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_task_lease import SchedulerTaskLeaseService


async def _seed_channel(Session, *, seed: int) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=171000 + seed,
            username=f"handoff-{seed}",
            full_name=f"Handoff {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(171100 + seed),
            title=f"Handoff {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        return int(channel.id), int(owner.tg_user_id)


async def _seed_bridge_publication(
    Session,
    *,
    seed: int,
    now: datetime,
    runtime_options: dict | None = None,
    repeat_rule: dict | None = None,
) -> tuple[int, int]:
    channel_id, owner_tg_id = await _seed_channel(Session, seed=seed)
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Canonical handoff {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=owner_tg_id,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options=runtime_options,
            repeat_rule=repeat_rule,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_pending_transport_handoff_serializes_before_canonical_claim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'handoff-success.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_bridge_publication(
                Session,
                seed=1,
                now=now,
            )

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(publication_id, at=now)
                assert result.outcome == "retired"
                assert result.legacy_post_task_id == task_id

            async with Session() as check_session:
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.attempt_count == 0
                assert publication.legacy_post_task_id is None
                assert await check_session.get(PostTask, task_id) is None
                schedule = await check_session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id),
                )
                assert schedule is not None
                assert "legacy_post_task_id" not in dict(schedule.meta or {})
                assert dict(publication.meta or {})[CUTOVER_META_KEY][
                    "legacy_post_task_id"
                ] == task_id
                assert dict(schedule.meta or {})[CUTOVER_META_KEY][
                    "legacy_post_task_id"
                ] == task_id

            # A stale legacy scheduler selection cannot claim the physically retired row.
            async with Session() as legacy_claim_session:
                assert (
                    await SchedulerTaskLeaseService(legacy_claim_session).claim_pending(
                        task_id=task_id,
                        holder="stale-legacy-worker",
                        ttl_seconds=60,
                        now=now,
                    )
                    is None
                )

            # Only after the handoff commit can the exact canonical claim take authority.
            async with Session() as canonical_claim_session:
                claim = await CanonicalPublicationDeliveryClaimService(
                    canonical_claim_session
                ).claim(
                    publication_id=publication_id,
                    holder="canonical-worker",
                    ttl_seconds=60,
                    now=now,
                    requirements=CanonicalPublicationDeliveryClaimRequirements(
                        require_empty_runtime_options=True,
                        require_nonrepeat=True,
                        require_transport_retired=True,
                    ),
                )
                assert claim is not None
                assert claim.publication_id if hasattr(claim, "publication_id") else True
                assert claim.plan.publication_id == publication_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_scheduler_claim_wins_before_handoff_and_blocks_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'handoff-legacy-wins.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_bridge_publication(
                Session,
                seed=2,
                now=now,
            )

            async with Session() as claim_session:
                legacy_handle = await SchedulerTaskLeaseService(
                    claim_session
                ).claim_pending(
                    task_id=task_id,
                    holder="legacy-worker",
                    ttl_seconds=120,
                    now=now,
                )
                assert legacy_handle is not None

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now + timedelta(seconds=1),
                )
                assert result.outcome == "contention"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                assert task is not None and task.status == "processing"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
                assert lease is not None
                assert lease.lease_token == legacy_handle.lease_token
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_any_scheduler_lease_including_expired_blocks_pending_handoff(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'handoff-expired-barrier.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_bridge_publication(
                Session,
                seed=3,
                now=now,
            )

            async with Session() as claim_session:
                handle = await SchedulerTaskLeaseService(claim_session).claim_pending(
                    task_id=task_id,
                    holder="legacy-worker",
                    ttl_seconds=60,
                    now=now - timedelta(minutes=2),
                )
                assert handle is not None

            # Simulate a historical/manual pending reset while the expired recovery
            # barrier remains. Handoff must roll its cutover CAS back instead of deleting it.
            async with Session() as reset_session:
                task = await reset_session.get(PostTask, task_id)
                assert task is not None
                task.status = "pending"
                await reset_session.commit()

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(publication_id, at=now)
                assert result.outcome == "conflict"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                lease = await SchedulerTaskLeaseService(check_session).current(task_id)
                assert task is not None and task.status == "pending"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
                assert lease is not None
                assert lease.lease_token == handle.lease_token
                assert lease.expires_at <= now
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_handoff_rejects_legacy_payload_drift_and_rolls_back_cutover_status(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'handoff-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_bridge_publication(
                Session,
                seed=4,
                now=now,
            )

            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["text"] = "legacy transport drifted after canonical snapshot"
                task.payload = payload
                await drift_session.commit()

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(publication_id, at=now)
                assert result.outcome == "ineligible"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mirrored_effectful_legacy_payload_cannot_hide_behind_empty_runtime_meta(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'handoff-hidden-effect.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)
            channel_id, _ = await _seed_channel(Session, seed=5)

            async with Session() as seed_session:
                task = PostTask(
                    channel_id=channel_id,
                    status="pending",
                    payload={
                        "type": "text",
                        "text": "legacy pin must stay legacy-owned",
                        "pin_on": True,
                    },
                    scheduled_at=now - timedelta(minutes=1),
                )
                seed_session.add(task)
                await seed_session.commit()
                await seed_session.refresh(task)
                task_id = int(task.id)
                publication = await mirror_legacy_post_task(seed_session, task)
                assert publication is not None
                publication_id = int(publication.id)

            async with Session() as proof_session:
                plan = await CanonicalPublicationDeliveryPlanner(proof_session).plan(
                    publication_id,
                    at=now,
                )
                assert plan is not None
                # Historical mirror metadata alone would incorrectly look plain.
                assert plan.runtime_options() == {}

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(publication_id, at=now)
                assert result.outcome == "ineligible"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())
