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
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(
    Session,
    *,
    seed: int,
    runtime_options: dict | None,
    repeat_rule: dict | None = None,
    retire_transport: bool = True,
) -> int:
    scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with Session() as session:
        owner = Client(
            tg_user_id=181000 + seed,
            username=f"silent-claim-{seed}",
            full_name=f"Silent Claim {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(181100 + seed),
            title=f"Silent Claim {seed}",
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
                        "text": f"Silent claim {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options=runtime_options,
            repeat_rule=repeat_rule,
        )
        publication_id = int(publication.id)
        if retire_transport:
            task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
            assert task is not None
            publication.legacy_post_task_id = None
            await session.delete(task)
            await session.commit()
        return publication_id


async def _assert_unclaimed(Session, publication_id: int) -> None:
    async with Session() as session:
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
        assert await session.get(PublicationDeliveryLease, publication_id) is None


def test_capability_claim_accepts_empty_and_explicit_boolean_silent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-capability-claim.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = [
                (1, {}, {}),
                (2, {"silent": True}, {"silent": True}),
                (3, {"silent": False}, {"silent": False}),
            ]
            for seed, runtime_options, expected in cases:
                publication_id = await _seed(
                    Session,
                    seed=seed,
                    runtime_options=runtime_options,
                )
                async with Session() as session:
                    claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                        session
                    ).claim_supported(
                        publication_id=publication_id,
                        holder="silent-capability-test",
                        ttl_seconds=180,
                    )
                    assert claim is not None
                    assert claim.plan.runtime_options() == expected
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    assert publication.status == "sending"
                    assert publication.attempt_count == 1
                    assert await session.get(
                        PublicationDeliveryLease, publication_id
                    ) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_capability_claim_rejects_malformed_or_unknown_options_before_writes(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-capability-reject.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            unsupported = [
                {"silent": 1},
                {"silent": "true"},
                {"pin_on": 1},
                {"forward_to": "1,2"},
                {"autodelete_seconds": 60},
            ]
            for index, runtime_options in enumerate(unsupported, start=10):
                publication_id = await _seed(
                    Session,
                    seed=index,
                    runtime_options=runtime_options,
                )
                async with Session() as session:
                    claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                        session
                    ).claim_supported(
                        publication_id=publication_id,
                        holder="unsupported-capability-test",
                        ttl_seconds=180,
                    )
                    assert claim is None
                await _assert_unclaimed(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_capability_claim_still_requires_transport_retired_and_nonrepeat(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-capability-authority.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            linked_id = await _seed(
                Session,
                seed=20,
                runtime_options={"silent": True},
                retire_transport=False,
            )
            async with Session() as linked_session:
                assert (
                    await CanonicalPublicationDeliveryCapabilityClaimService(
                        linked_session
                    ).claim_supported(
                        publication_id=linked_id,
                        holder="linked",
                        ttl_seconds=180,
                    )
                    is None
                )
            await _assert_unclaimed(Session, linked_id)

            repeat_id = await _seed(
                Session,
                seed=21,
                runtime_options={"silent": True},
                repeat_rule={"enabled": True, "seconds": 300},
            )
            async with Session() as repeat_session:
                assert (
                    await CanonicalPublicationDeliveryCapabilityClaimService(
                        repeat_session
                    ).claim_supported(
                        publication_id=repeat_id,
                        holder="repeat",
                        ttl_seconds=180,
                    )
                    is None
                )
            await _assert_unclaimed(Session, repeat_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
