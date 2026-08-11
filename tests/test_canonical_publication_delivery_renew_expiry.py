from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.publication_delivery import PublicationDeliveryLease
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
    CanonicalPublicationDeliveryLeaseHandle,
)


def test_delivery_renew_cannot_revive_expired_ownership(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-renew-expiry.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            expires_at = datetime(2026, 8, 11, 12, 3, tzinfo=timezone.utc)
            publication_id = 991001
            token = "normal-worker-token"

            async with Session() as session:
                session.add(
                    PublicationDeliveryLease(
                        publication_id=publication_id,
                        lease_token=token,
                        holder="normal-worker",
                        expires_at=expires_at,
                    )
                )
                await session.commit()

                service = CanonicalPublicationDeliveryClaimService(session)
                handle = CanonicalPublicationDeliveryLeaseHandle(
                    publication_id=publication_id,
                    lease_token=token,
                    holder="normal-worker",
                    expires_at=expires_at,
                )

                live = await service.renew(
                    handle,
                    ttl_seconds=30,
                    now=expires_at - timedelta(seconds=1),
                )
                assert live is not None
                assert live.expires_at == expires_at + timedelta(seconds=29)

                row = await service.current(publication_id)
                assert row is not None
                row.expires_at = expires_at
                await session.commit()

                at_boundary = await service.renew(
                    handle,
                    ttl_seconds=180,
                    now=expires_at,
                )
                assert at_boundary is None
                row = await service.current(publication_id)
                assert row is not None
                assert row.expires_at == expires_at

                after_expiry = await service.renew(
                    handle,
                    ttl_seconds=180,
                    now=expires_at + timedelta(seconds=1),
                )
                assert after_expiry is None
                row = await service.current(publication_id)
                assert row is not None
                assert row.expires_at == expires_at
        finally:
            await engine.dispose()

    asyncio.run(run())
