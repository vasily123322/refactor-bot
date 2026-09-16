from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_errors import GENERIC_SCHEDULER_ERROR


async def _seed_claimable_publication(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=119000 + seed,
            username=f"canonical-finalizer-{seed}",
            full_name=f"Canonical Finalizer {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(119100 + seed),
            title=f"Canonical Finalizer {seed}",
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
                        "text": "Canonical finalizer proof",
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
        return int(publication.id), int(publication.legacy_post_task_id or 0)


async def _claim_after_transport_retirement(
    Session,
    *,
    publication_id: int,
    task_id: int,
    now: datetime,
):
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        assert publication is not None
        publication.legacy_post_task_id = None
        if task is not None:
            await session.delete(task)
        await session.commit()
        claim = await CanonicalPublicationDeliveryClaimService(session).claim(
            publication_id=publication_id,
            holder="finalizer-test",
            now=now,
        )
        assert claim is not None
        return claim


def _as_utc(value: datetime) -> datetime:
    # SQLite may round-trip timezone-aware DateTime values as naive UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_success_finalization_is_canonical_only_and_releases_exact_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-finalizer-success.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_claimable_publication(
                Session,
                seed=1,
                scheduled_at=scheduled_at,
            )
            claim = await _claim_after_transport_retirement(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=scheduled_at + timedelta(minutes=1),
            )
            finished_at = scheduled_at + timedelta(minutes=2)

            async with Session() as session:
                finalizer = CanonicalPublicationDeliveryFinalizer(session)
                result = await finalizer.complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[501, 502],
                    result_link="https://t.me/c/12345/502",
                    finished_at=finished_at,
                )
                assert result.outcome == "published"
                assert result.attempt == 1

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.attempt_count == 1
                assert publication.telegram_message_ids == [501, 502]
                assert publication.result_link == "https://t.me/c/12345/502"
                assert publication.last_error is None
                assert publication.legacy_post_task_id is None

                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert schedule.status == "completed"

                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "published"
                assert attempt.telegram_message_ids == [501, 502]
                assert attempt.error is None
                assert attempt.finished_at is not None
                assert _as_utc(attempt.finished_at) == finished_at
                assert await CanonicalPublicationDeliveryClaimService(session).current(
                    publication_id
                ) is None

                stale = await finalizer.complete_failure(
                    claim.lease,
                    error="late worker",
                    finished_at=finished_at + timedelta(seconds=1),
                )
                assert stale.outcome == "conflict"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_failure_finalization_redacts_untrusted_error_text(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-finalizer-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_claimable_publication(
                Session,
                seed=2,
                scheduled_at=scheduled_at,
            )
            claim = await _claim_after_transport_retirement(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=scheduled_at + timedelta(minutes=1),
            )
            provider_error = "https://api.telegram.org/botSUPERSECRET/sendMessage failed"

            async with Session() as session:
                result = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_failure(
                    claim.lease,
                    error=provider_error,
                    finished_at=scheduled_at + timedelta(minutes=2),
                )
                assert result.outcome == "failed"

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "failed"
                assert publication.telegram_message_ids is None
                assert publication.result_link is None
                assert publication.last_error == GENERIC_SCHEDULER_ERROR
                assert "SUPERSECRET" not in publication.last_error

                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert schedule.status == "failed"

                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "failed"
                assert attempt.error == GENERIC_SCHEDULER_ERROR
                assert attempt.finished_at is not None
                assert await CanonicalPublicationDeliveryClaimService(session).current(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_lease_token_cannot_finalize_or_release_live_claim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-finalizer-stale.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_claimable_publication(
                Session,
                seed=3,
                scheduled_at=scheduled_at,
            )
            claim = await _claim_after_transport_retirement(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=scheduled_at + timedelta(minutes=1),
            )
            stale = CanonicalPublicationDeliveryLeaseHandle(
                publication_id=publication_id,
                lease_token="stale-token",
                holder="stale-worker",
                expires_at=claim.lease.expires_at,
            )

            async with Session() as session:
                finalizer = CanonicalPublicationDeliveryFinalizer(session)
                result = await finalizer.complete_success(
                    stale,
                    plan=claim.plan,
                    message_ids=[601],
                    finished_at=scheduled_at + timedelta(minutes=2),
                )
                assert result.outcome == "conflict"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
                current = await CanonicalPublicationDeliveryClaimService(session).current(
                    publication_id
                )
                assert current is not None
                assert current.lease_token == claim.lease.lease_token

                exact = await finalizer.complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[601],
                    finished_at=scheduled_at + timedelta(minutes=2),
                )
                assert exact.outcome == "published"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_success_evidence_keeps_claim_open_for_explicit_resolution(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-finalizer-invalid.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_claimable_publication(
                Session,
                seed=4,
                scheduled_at=scheduled_at,
            )
            claim = await _claim_after_transport_retirement(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=scheduled_at + timedelta(minutes=1),
            )

            async with Session() as session:
                finalizer = CanonicalPublicationDeliveryFinalizer(session)
                empty = await finalizer.complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[],
                )
                assert empty.outcome == "invalid"
                unsafe_link = await finalizer.complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[701],
                    result_link="https://evil.example/secret/701",
                )
                assert unsafe_link.outcome == "invalid"

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "sending"
                assert attempt.finished_at is None
                assert await CanonicalPublicationDeliveryClaimService(session).current(
                    publication_id
                ) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
