from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_inflight_audit import (
    ATTEMPT_MISMATCH,
    DURABLE_DELIVERY_EVIDENCE,
    EXPIRED_LEASE,
    HEALTHY_LIVE_LEASE,
    LEGACY_TRANSPORT_RELINKED,
    MISSING_LEASE,
    SCHEDULE_MISMATCH,
    CanonicalPublicationDeliveryInflightAuditService,
)


async def _seed_sending(
    Session,
    *,
    seed: int,
    now: datetime,
    lease_state: str = "live",
    legacy_linked: bool = False,
    schedule_status: str = "pending",
    canonical_attempt: bool = True,
    durable_evidence: bool = False,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=151000 + seed,
            username=f"inflight-audit-{seed}",
            full_name=f"Inflight Audit {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(151100 + seed),
            title=f"Inflight Audit {seed}",
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
                        "text": f"Inflight audit {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=int(item.current_revision),
            channel_id=int(channel.id),
            scheduled_at=now - timedelta(minutes=1),
            timezone="UTC",
            status=schedule_status,
            repeat_rule={},
            meta={},
        )
        session.add(schedule)
        await session.flush()

        legacy_post_task_id = None
        if legacy_linked:
            task = PostTask(
                channel_id=int(channel.id),
                status="pending",
                payload={},
                dedupe_key=f"inflight-audit-{seed}",
                scheduled_at=now - timedelta(minutes=1),
                error=None,
            )
            session.add(task)
            await session.flush()
            legacy_post_task_id = int(task.id)

        publication = Publication(
            schedule_entry_id=int(schedule.id),
            content_item_id=int(item.id),
            content_revision=int(item.current_revision),
            channel_id=int(channel.id),
            status="sending",
            legacy_post_task_id=legacy_post_task_id,
            telegram_message_ids=([9000 + seed] if durable_evidence else None),
            result_link=None,
            last_error=None,
            attempt_count=1,
            meta={},
        )
        session.add(publication)
        await session.flush()
        publication_id = int(publication.id)
        session.add(
            PublicationAttempt(
                publication_id=publication_id,
                attempt=1,
                status="sending",
                telegram_message_ids=None,
                error=None,
                meta={"canonical_delivery": canonical_attempt},
                finished_at=None,
            )
        )
        if lease_state != "missing":
            expires_at = (
                now + timedelta(minutes=1)
                if lease_state == "live"
                else now
            )
            session.add(
                PublicationDeliveryLease(
                    publication_id=publication_id,
                    lease_token=f"secret-audit-token-{seed}",
                    holder="canonical-publication-delivery",
                    expires_at=expires_at,
                )
            )
        await session.commit()
        return publication_id


def test_inflight_audit_reports_healthy_live_state_without_exposing_token(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'inflight-audit-healthy.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
            publication_id = await _seed_sending(Session, seed=1, now=now)

            async with Session() as session:
                page = await CanonicalPublicationDeliveryInflightAuditService(
                    session
                ).scan_page(now=now)

                assert page.done is True
                assert page.next_publication_id is None
                assert len(page.items) == 1
                item = page.items[0]
                assert item.publication_id == publication_id
                assert item.classification == HEALTHY_LIVE_LEASE
                assert item.findings == ()
                assert item.lease_state == "live"
                assert not hasattr(item, "lease_token")

                publication = await session.get(Publication, publication_id)
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert publication is not None and publication.status == "sending"
                assert lease is not None
                assert lease.lease_token == "secret-audit-token-1"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_inflight_audit_treats_exact_ttl_boundary_as_expired(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'inflight-audit-expired.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
            await _seed_sending(Session, seed=2, now=now, lease_state="expired")

            async with Session() as session:
                item = (
                    await CanonicalPublicationDeliveryInflightAuditService(
                        session
                    ).scan_page(now=now)
                ).items[0]
                assert item.classification == EXPIRED_LEASE
                assert item.findings == (EXPIRED_LEASE,)
                assert item.lease_state == "expired"
                assert item.lease_expires_at == now
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_inflight_audit_reports_missing_lease_without_repair(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'inflight-audit-missing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
            publication_id = await _seed_sending(
                Session,
                seed=3,
                now=now,
                lease_state="missing",
            )

            async with Session() as session:
                item = (
                    await CanonicalPublicationDeliveryInflightAuditService(
                        session
                    ).scan_page(now=now)
                ).items[0]
                assert item.classification == MISSING_LEASE
                assert item.findings == (MISSING_LEASE,)
                assert item.lease_state == "missing"
                assert await session.get(PublicationDeliveryLease, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_inflight_audit_preserves_multiple_corrupt_findings(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'inflight-audit-corrupt.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
            publication_id = await _seed_sending(
                Session,
                seed=4,
                now=now,
                lease_state="missing",
                legacy_linked=True,
                schedule_status="completed",
                canonical_attempt=False,
                durable_evidence=True,
            )

            async with Session() as session:
                item = (
                    await CanonicalPublicationDeliveryInflightAuditService(
                        session
                    ).scan_page(now=now)
                ).items[0]
                assert item.publication_id == publication_id
                assert item.classification == LEGACY_TRANSPORT_RELINKED
                assert item.findings == (
                    LEGACY_TRANSPORT_RELINKED,
                    DURABLE_DELIVERY_EVIDENCE,
                    SCHEDULE_MISMATCH,
                    ATTEMPT_MISMATCH,
                    MISSING_LEASE,
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_inflight_audit_uses_bounded_keyset_pagination(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'inflight-audit-pagination.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
            first_id = await _seed_sending(Session, seed=5, now=now)
            second_id = await _seed_sending(Session, seed=6, now=now)

            async with Session() as session:
                audit = CanonicalPublicationDeliveryInflightAuditService(session)
                first = await audit.scan_page(limit=1, now=now)
                assert first.done is False
                assert [item.publication_id for item in first.items] == [first_id]
                assert first.next_publication_id == first_id

                second = await audit.scan_page(
                    limit=1,
                    after_publication_id=first.next_publication_id,
                    now=now,
                )
                assert second.done is True
                assert second.next_publication_id is None
                assert [item.publication_id for item in second.items] == [second_id]
        finally:
            await engine.dispose()

    asyncio.run(run())
