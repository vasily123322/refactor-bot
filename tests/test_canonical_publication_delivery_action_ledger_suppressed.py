from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_action_ledger import (
    CanonicalPublicationDeliveryActionLedger,
)
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.publication_bridge import LegacyPublicationBridge


def test_suppressed_action_is_terminal_and_never_reauthorized(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'action-ledger-suppressed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 11, 15, 45, tzinfo=timezone.utc)

            async with Session() as session:
                owner = Client(
                    tg_user_id=188001,
                    username="action-suppressed",
                    full_name="Action Suppressed",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-100188001,
                    title="Action Suppressed",
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
                                "text": "Suppressed action proof",
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
                task = await session.get(
                    PostTask,
                    int(publication.legacy_post_task_id or 0),
                )
                publication.legacy_post_task_id = None
                if task is not None:
                    await session.delete(task)
                await session.commit()
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=int(publication.id),
                    holder="suppressed-test",
                    ttl_seconds=180,
                    now=now,
                )
                assert claim is not None

            fingerprint = hashlib.sha256(b"suppressed-forward").hexdigest()
            async with Session() as session:
                reserved = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    claim.lease,
                    action_key="forward:99:2401",
                    action_type="forward",
                    intent_fingerprint=fingerprint,
                    now=now + timedelta(seconds=1),
                )
                assert reserved.reservation is not None
                reservation = reserved.reservation

            async with Session() as session:
                ledger = CanonicalPublicationDeliveryActionLedger(session)
                assert await ledger.mark_suppressed(
                    reservation,
                    finished_at=now + timedelta(seconds=2),
                ) is True
                assert await ledger.mark_succeeded(
                    reservation,
                    finished_at=now + timedelta(seconds=3),
                ) is False

            async with Session() as session:
                repeated = await CanonicalPublicationDeliveryActionLedger(session).reserve(
                    claim.lease,
                    action_key="forward:99:2401",
                    action_type="forward",
                    intent_fingerprint=fingerprint,
                    now=now + timedelta(seconds=4),
                )
                assert repeated.outcome == "already_reserved"
                assert repeated.existing_state == "suppressed"
        finally:
            await engine.dispose()

    asyncio.run(run())
