from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.scheduler_errors import UNKNOWN_DELIVERY_ERROR


def test_expired_delivery_lease_requires_recovery_ownership_before_terminal_state(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-finalizer-expired.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            expired_at = scheduled_at + timedelta(minutes=2)

            async with Session() as session:
                owner = Client(
                    tg_user_id=119999,
                    username="canonical-finalizer-expiry",
                    full_name="Canonical Finalizer Expiry",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-119999,
                    title="Canonical Finalizer Expiry",
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
                                "text": "Expired canonical delivery",
                            }
                        ]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                schedule = ScheduleEntry(
                    content_item_id=int(item.id),
                    content_revision=int(item.current_revision),
                    channel_id=int(channel.id),
                    scheduled_at=scheduled_at,
                    timezone="UTC",
                    status="pending",
                    repeat_rule={},
                    meta={},
                )
                session.add(schedule)
                await session.flush()
                publication = Publication(
                    schedule_entry_id=int(schedule.id),
                    content_item_id=int(item.id),
                    content_revision=int(item.current_revision),
                    channel_id=int(channel.id),
                    status="sending",
                    legacy_post_task_id=None,
                    telegram_message_ids=None,
                    result_link=None,
                    last_error=None,
                    attempt_count=1,
                    meta={},
                )
                session.add(publication)
                await session.flush()
                publication_id = int(publication.id)
                session.add_all(
                    [
                        PublicationAttempt(
                            publication_id=publication_id,
                            attempt=1,
                            status="sending",
                            telegram_message_ids=None,
                            error=None,
                            meta={"canonical_delivery": True},
                            finished_at=None,
                        ),
                        PublicationDeliveryLease(
                            publication_id=publication_id,
                            lease_token="expired-worker-token",
                            holder="expired-worker",
                            expires_at=expired_at,
                        ),
                    ]
                )
                await session.commit()

            stale = CanonicalPublicationDeliveryLeaseHandle(
                publication_id=publication_id,
                lease_token="expired-worker-token",
                holder="expired-worker",
                expires_at=expired_at,
            )
            recovery_now = expired_at + timedelta(seconds=1)

            async with Session() as session:
                finalizer = CanonicalPublicationDeliveryFinalizer(session)
                stale_result = await finalizer.complete_success(
                    stale,
                    message_ids=[901],
                    now=recovery_now,
                    finished_at=recovery_now,
                )
                assert stale_result.outcome == "conflict"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"

                claims = CanonicalPublicationDeliveryClaimService(session)
                expired = await claims.expired(now=recovery_now)
                assert len(expired) == 1
                recovery = await claims.take_expired(
                    expired[0],
                    holder="recovery-worker",
                    now=recovery_now,
                )
                assert recovery is not None
                assert recovery.lease_token != stale.lease_token

                resolved = await finalizer.complete_failure(
                    recovery,
                    error=UNKNOWN_DELIVERY_ERROR,
                    now=recovery_now + timedelta(seconds=1),
                    finished_at=recovery_now + timedelta(seconds=1),
                )
                assert resolved.outcome == "failed"

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "failed"
                assert publication.last_error == UNKNOWN_DELIVERY_ERROR
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert schedule.status == "failed"
                attempt = await session.get(PublicationAttempt, 1)
                assert attempt is not None
                assert attempt.status == "failed"
                assert attempt.error == UNKNOWN_DELIVERY_ERROR
                assert attempt.finished_at is not None
                assert await claims.current(publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
