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
from app.services.canonical_publication_delivery_candidates import (
    CanonicalPublicationDeliveryCandidateSelector,
)
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimRequirements,
    CanonicalPublicationDeliveryClaimService,
)
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_linked_due_publication(Session, *, seed: int) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=138000 + seed,
            username=f"transport-authority-{seed}",
            full_name=f"Transport Authority {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100138000 + seed),
            title=f"Transport Authority {seed}",
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
                        "text": "Transport authority proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={},
        )
        task_id = int(publication.legacy_post_task_id or 0)
        assert task_id > 0
        assert await session.get(PostTask, task_id) is not None
        return int(publication.id), task_id


async def _assert_unclaimed(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        assert publication.status == "queued"
        assert publication.attempt_count == 0
        assert int(publication.legacy_post_task_id or 0) == task_id
        assert await session.get(PostTask, task_id) is not None
        assert await session.get(PublicationDeliveryLease, publication_id) is None
        attempts = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id
                )
            )
        ).scalars().all()
        assert attempts == []


async def _retire_transport(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        assert publication is not None and task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()


class _Sender:
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
        return [2201]


def test_transport_retirement_requirement_is_atomic_and_optional_for_generic_claim(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'transport-authority-claim.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked_due_publication(Session, seed=1)
            current = datetime.now(timezone.utc)

            async with Session() as session:
                selector = CanonicalPublicationDeliveryCandidateSelector(session)
                candidates = await selector.due(limit=10, at=current)
                assert publication_id in {
                    candidate.publication_id for candidate in candidates
                }

                blocked = await CanonicalPublicationDeliveryClaimService(session).claim(
                    publication_id=publication_id,
                    holder="transport-authority-test",
                    now=current,
                    requirements=CanonicalPublicationDeliveryClaimRequirements(
                        require_transport_retired=True,
                    ),
                )
                assert blocked is None

            await _assert_unclaimed(Session, publication_id, task_id)
            await _retire_transport(Session, publication_id, task_id)

            async with Session() as session:
                claimed = await CanonicalPublicationDeliveryClaimService(session).claim(
                    publication_id=publication_id,
                    holder="transport-authority-test",
                    now=current + timedelta(seconds=1),
                    requirements=CanonicalPublicationDeliveryClaimRequirements(
                        require_transport_retired=True,
                    ),
                )
                assert claimed is not None
                assert claimed.plan.publication_id == publication_id

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, task_id) is None
                assert await session.get(PublicationDeliveryLease, publication_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concrete_executor_never_calls_provider_while_legacy_transport_is_linked(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'transport-authority-executor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked_due_publication(Session, seed=2)
            sender = _Sender()
            executor = CanonicalPublicationDeliveryExecutor(Session, sender=sender)

            blocked = await executor.execute(publication_id)
            assert blocked.outcome == "ineligible"
            assert sender.calls == 0
            await _assert_unclaimed(Session, publication_id, task_id)

            await _retire_transport(Session, publication_id, task_id)
            published = await executor.execute(publication_id)
            assert published.outcome == "published"
            assert published.message_ids == (2201,)
            assert sender.calls == 1

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.telegram_message_ids == [2201]
                assert publication.legacy_post_task_id is None
                assert await session.get(PublicationDeliveryLease, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
