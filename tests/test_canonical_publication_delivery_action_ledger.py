from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryAction
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_action_ledger import (
    CanonicalPublicationDeliveryActionLedger,
)
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.publication_bridge import LegacyPublicationBridge


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _claimed(Session, *, seed: int, now: datetime, ttl_seconds: int = 180):
    async with Session() as session:
        owner = Client(
            tg_user_id=186000 + seed,
            username=f"action-ledger-{seed}",
            full_name=f"Action Ledger {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(186100 + seed),
            title=f"Action Ledger {seed}",
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
                        "text": f"Action ledger {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options={},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        publication.legacy_post_task_id = None
        if task is not None:
            await session.delete(task)
        await session.commit()

        claim = await CanonicalPublicationDeliveryCapabilityClaimService(
            session
        ).claim_supported(
            publication_id=int(publication.id),
            holder="action-ledger-test",
            ttl_seconds=ttl_seconds,
            now=now,
        )
        assert claim is not None
        return int(publication.id), claim.lease


def test_action_ledger_reserves_once_and_never_reauthorizes_same_action(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'action-ledger-once.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
            publication_id, lease = await _claimed(Session, seed=1, now=now)
            fingerprint = _fingerprint("pin:source-chat:last-message")

            async with Session() as session:
                first = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="pin:2201",
                    action_type="pin",
                    intent_fingerprint=fingerprint,
                    now=now + timedelta(seconds=1),
                )
                assert first.outcome == "reserved"
                assert first.reservation is not None
                assert first.reservation.delivery_lease_token == lease.lease_token

            async with Session() as session:
                second = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="pin:2201",
                    action_type="pin",
                    intent_fingerprint=fingerprint,
                    now=now + timedelta(seconds=2),
                )
                assert second.outcome == "already_reserved"
                assert second.reservation is None
                assert second.existing_state == "reserved"

                rows = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].state == "reserved"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_action_ledger_conflicting_fingerprint_never_gets_second_reservation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'action-ledger-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
            publication_id, lease = await _claimed(Session, seed=2, now=now)

            async with Session() as session:
                first = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="forward:71:2202",
                    action_type="forward",
                    intent_fingerprint=_fingerprint("target=71;chat=-10071;silent=0"),
                    now=now + timedelta(seconds=1),
                )
                assert first.outcome == "reserved"

            async with Session() as session:
                conflict = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="forward:71:2202",
                    action_type="forward",
                    intent_fingerprint=_fingerprint("target=71;chat=-100999;silent=0"),
                    now=now + timedelta(seconds=2),
                )
                assert conflict.outcome == "conflict"
                rows = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert len(rows) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_action_ledger_terminal_evidence_is_one_way_and_does_not_reopen(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'action-ledger-terminal.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
            _publication_id, lease = await _claimed(Session, seed=3, now=now)
            fingerprint = _fingerprint("pin-terminal")

            async with Session() as session:
                reserved = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="pin:2203",
                    action_type="pin",
                    intent_fingerprint=fingerprint,
                    now=now + timedelta(seconds=1),
                )
                assert reserved.reservation is not None
                reservation = reserved.reservation

            async with Session() as session:
                ledger = CanonicalPublicationDeliveryActionLedger(session)
                assert await ledger.mark_succeeded(
                    reservation,
                    finished_at=now + timedelta(seconds=2),
                ) is True
                assert await ledger.mark_succeeded(
                    reservation,
                    finished_at=now + timedelta(seconds=3),
                ) is True
                assert await ledger.mark_unknown(
                    reservation,
                    finished_at=now + timedelta(seconds=4),
                ) is False

            async with Session() as session:
                repeated = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="pin:2203",
                    action_type="pin",
                    intent_fingerprint=fingerprint,
                    now=now + timedelta(seconds=5),
                )
                assert repeated.outcome == "already_reserved"
                assert repeated.existing_state == "succeeded"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_action_ledger_unknown_state_is_a_permanent_no_retry_barrier(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'action-ledger-unknown.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
            _publication_id, lease = await _claimed(Session, seed=4, now=now)
            fingerprint = _fingerprint("forward-unknown")

            async with Session() as session:
                result = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="forward:72:2204",
                    action_type="forward",
                    intent_fingerprint=fingerprint,
                    now=now + timedelta(seconds=1),
                )
                assert result.reservation is not None
                reservation = result.reservation

            async with Session() as session:
                assert await CanonicalPublicationDeliveryActionLedger(
                    session
                ).mark_unknown(
                    reservation,
                    finished_at=now + timedelta(seconds=2),
                ) is True

            async with Session() as session:
                repeated = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="forward:72:2204",
                    action_type="forward",
                    intent_fingerprint=fingerprint,
                    now=now + timedelta(seconds=3),
                )
                assert repeated.outcome == "already_reserved"
                assert repeated.existing_state == "unknown"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_action_ledger_requires_exact_live_delivery_lease_at_ttl_boundary(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'action-ledger-lease.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
            publication_id, lease = await _claimed(
                Session,
                seed=5,
                now=now,
                ttl_seconds=30,
            )

            async with Session() as boundary_session:
                boundary = await CanonicalPublicationDeliveryActionLedger(
                    boundary_session
                ).reserve(
                    lease,
                    action_key="pin:2205",
                    action_type="pin",
                    intent_fingerprint=_fingerprint("boundary"),
                    now=lease.expires_at,
                )
                assert boundary.outcome == "ineligible"

            stale = replace(lease, lease_token="stale-token")
            async with Session() as stale_session:
                rejected = await CanonicalPublicationDeliveryActionLedger(
                    stale_session
                ).reserve(
                    stale,
                    action_key="forward:73:2205",
                    action_type="forward",
                    intent_fingerprint=_fingerprint("stale"),
                    now=now + timedelta(seconds=1),
                )
                assert rejected.outcome == "ineligible"

                rows = (
                    await stale_session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_action_ledger_rejects_lifecycle_evidence_before_reservation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'action-ledger-evidence.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
            publication_id, lease = await _claimed(Session, seed=6, now=now)

            async with Session() as drift_session:
                publication = await drift_session.get(Publication, publication_id)
                assert publication is not None
                publication.result_link = "https://t.me/c/186106/2206"
                await drift_session.commit()

            async with Session() as session:
                result = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    lease,
                    action_key="pin:2206",
                    action_type="pin",
                    intent_fingerprint=_fingerprint("evidence"),
                    now=now + timedelta(seconds=1),
                )
                assert result.outcome == "ineligible"
                assert await session.get(
                    PublicationDeliveryAction,
                    {"publication_id": publication_id, "action_key": "pin:2206"},
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
