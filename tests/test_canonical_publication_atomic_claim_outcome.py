from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_atomic_claim_outcome import (
    CanonicalPublicationAtomicClaimFailureClassifier,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_linked(Session, *, seed: int) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=197000 + seed,
            username=f"claim-outcome-{seed}",
            full_name=f"Claim Outcome {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100197000 + seed),
            title=f"Claim Outcome {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Outcome {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={
                "autodelete_seconds": 3600,
                "autodelete_views": 100,
            },
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_exact_rolled_back_linked_state_is_claim_rejected(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'claim-rejected.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked(Session, seed=1)

            async with Session() as session:
                result = await CanonicalPublicationAtomicClaimFailureClassifier(
                    session
                ).classify(
                    publication_id=publication_id,
                    legacy_post_task_id=task_id,
                )
                assert result.outcome == "claim_rejected"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_committed_sending_lease_state_is_claim_unavailable(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'claim-unavailable.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked(Session, seed=2)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.status = "sending"
                publication.attempt_count = 1
                publication.legacy_post_task_id = None
                session.add(
                    PublicationAttempt(
                        publication_id=publication_id,
                        attempt=1,
                        status="sending",
                        telegram_message_ids=None,
                        error=None,
                        meta={"canonical_delivery": True},
                        finished_at=None,
                    )
                )
                session.add(
                    PublicationDeliveryLease(
                        publication_id=publication_id,
                        lease_token="outcome-lease-2",
                        holder="outcome-test",
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
                    )
                )
                await session.delete(task)
                await session.commit()

            async with Session() as session:
                result = await CanonicalPublicationAtomicClaimFailureClassifier(
                    session
                ).classify(
                    publication_id=publication_id,
                    legacy_post_task_id=task_id,
                )
                assert result.outcome == "claim_unavailable"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_any_partial_or_inconsistent_state_is_never_retryable(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'claim-partial.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked(Session, seed=3)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "processing"
                await session.commit()

            async with Session() as session:
                result = await CanonicalPublicationAtomicClaimFailureClassifier(
                    session
                ).classify(
                    publication_id=publication_id,
                    legacy_post_task_id=task_id,
                )
                assert result.outcome == "claim_unavailable"
        finally:
            await engine.dispose()

    asyncio.run(run())
