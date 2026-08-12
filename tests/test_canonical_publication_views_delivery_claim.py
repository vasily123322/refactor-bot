from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_retired_views(Session, *, seed: int, threshold: int = 100) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=201000 + seed,
            username=f"views-claim-{seed}",
            full_name=f"Views Claim {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100201000 + seed),
            title=f"Views Claim {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Views claim {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={
                "autodelete_views": threshold,
                "autodelete_report": True,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_views_claim_is_default_off_and_stages_threshold_in_same_authority_commit(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-claim.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            disabled_id = await _seed_retired_views(Session, seed=1, threshold=80)

            async with Session() as session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=disabled_id,
                    holder="views-disabled",
                    ttl_seconds=180,
                    allow_views_autodelete=False,
                )
                assert claim is None

            async with Session() as session:
                publication = await session.get(Publication, disabled_id)
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
                assert await session.get(PublicationAutodeleteViewState, disabled_id) is None
                assert await session.get(PublicationDeliveryLease, disabled_id) is None

            enabled_id = await _seed_retired_views(Session, seed=2, threshold=125)
            claim_at = datetime.now(timezone.utc)
            async with Session() as session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=enabled_id,
                    holder="views-enabled",
                    ttl_seconds=180,
                    now=claim_at,
                    allow_views_autodelete=True,
                )
                assert claim is not None
                assert claim.plan.runtime_options()["autodelete_views"] == 125

            async with Session() as session:
                publication = await session.get(Publication, enabled_id)
                state = await session.get(PublicationAutodeleteViewState, enabled_id)
                lease = await session.get(PublicationDeliveryLease, enabled_id)
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == enabled_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert publication is not None
                assert publication.status == "sending"
                assert int(publication.attempt_count or 0) == 1
                assert state is not None
                assert int(state.threshold) == 125
                assert state.next_check_at is not None
                assert lease is not None
                assert attempt.status == "sending"
                assert dict(attempt.meta or {}).get("canonical_delivery") is True

                # Indexed intent is already durable before provider work, but destructive
                # selection cannot expose a still-sending Publication.
                due = await PublicationAutodeleteViewStateService(
                    session
                ).select_due_publication_ids(
                    now=claim_at + timedelta(minutes=1),
                    limit=100,
                )
                assert enabled_id not in due.publication_ids

            async with Session() as session:
                finalized = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[5101],
                    result_link=None,
                    finished_at=claim_at + timedelta(seconds=2),
                    now=claim_at + timedelta(seconds=3),
                )
                assert finalized.outcome == "published"

            async with Session() as session:
                due = await PublicationAutodeleteViewStateService(
                    session
                ).select_due_publication_ids(
                    now=claim_at + timedelta(minutes=1),
                    limit=100,
                )
                assert enabled_id in due.publication_ids
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_views_state_staging_rolls_back_when_lower_claim_commit_conflicts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-claim-rollback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired_views(Session, seed=3, threshold=240)
            now = datetime.now(timezone.utc)

            # A pre-existing canonical lease forces the lower claim's lease insert to
            # fail at the authority commit. The staged views row must be part of the same
            # rollback, not survive as an orphaned destructive trigger.
            async with Session() as session:
                session.add(
                    PublicationDeliveryLease(
                        publication_id=publication_id,
                        lease_token="existing-views-lease",
                        holder="conflict",
                        expires_at=now + timedelta(minutes=5),
                    )
                )
                await session.commit()

            async with Session() as session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="views-conflict",
                    ttl_seconds=180,
                    now=now,
                    allow_views_autodelete=True,
                )
                assert claim is None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
                assert await session.get(PublicationAutodeleteViewState, publication_id) is None
                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert attempts == []
                existing = await session.get(PublicationDeliveryLease, publication_id)
                assert existing is not None
                assert existing.lease_token == "existing-views-lease"
        finally:
            await engine.dispose()

    asyncio.run(run())
