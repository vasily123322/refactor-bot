from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_autodelete import PublicationAutodeleteLease
from app.domain.publishing.models import Publication


DEFAULT_PUBLICATION_AUTODELETE_LEASE_SECONDS = 180


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteLeaseHandle:
    publication_id: int
    lease_token: str
    holder: str
    expires_at: datetime


class PublicationAutodeleteLeaseService:
    """Cross-process ownership lease for canonical-only autodelete attempts.

    Expired leases are reclaimable because the protected provider operation is
    delete-only and idempotent at this boundary: a retry sees already-removed Telegram
    messages as terminal-unavailable and can safely finish canonical synchronization.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def acquire(
        self,
        *,
        publication_id: int,
        holder: str,
        ttl_seconds: int = DEFAULT_PUBLICATION_AUTODELETE_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> PublicationAutodeleteLeaseHandle | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        current = _utc(now)
        ttl = max(30, min(int(ttl_seconds), 600))
        expires_at = current + timedelta(seconds=ttl)
        token = uuid.uuid4().hex
        holder_value = str(holder).strip()[:64] or "autodelete"

        try:
            eligible_id = (
                await self.session.execute(
                    select(Publication.id).where(
                        Publication.id == safe_publication_id,
                        Publication.legacy_post_task_id.is_(None),
                        Publication.status == "published",
                    )
                )
            ).scalar_one_or_none()
            if eligible_id is None:
                await self.session.rollback()
                return None

            await self.session.execute(
                delete(PublicationAutodeleteLease).where(
                    PublicationAutodeleteLease.publication_id == safe_publication_id,
                    PublicationAutodeleteLease.expires_at <= current,
                )
            )
            self.session.add(
                PublicationAutodeleteLease(
                    publication_id=safe_publication_id,
                    lease_token=token,
                    holder=holder_value,
                    expires_at=expires_at,
                )
            )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return None
        except Exception:
            await self.session.rollback()
            raise

        return PublicationAutodeleteLeaseHandle(
            publication_id=safe_publication_id,
            lease_token=token,
            holder=holder_value,
            expires_at=expires_at,
        )

    async def renew(
        self,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        ttl_seconds: int = DEFAULT_PUBLICATION_AUTODELETE_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> PublicationAutodeleteLeaseHandle | None:
        current = _utc(now)
        ttl = max(30, min(int(ttl_seconds), 600))
        expires_at = current + timedelta(seconds=ttl)
        result = await self.session.execute(
            update(PublicationAutodeleteLease)
            .where(
                PublicationAutodeleteLease.publication_id == int(handle.publication_id),
                PublicationAutodeleteLease.lease_token == str(handle.lease_token),
            )
            .values(expires_at=expires_at)
            .execution_options(synchronize_session=False)
        )
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        if int(result.rowcount or 0) != 1:
            return None
        return PublicationAutodeleteLeaseHandle(
            publication_id=int(handle.publication_id),
            lease_token=str(handle.lease_token),
            holder=str(handle.holder),
            expires_at=expires_at,
        )

    async def release(self, handle: PublicationAutodeleteLeaseHandle) -> bool:
        result = await self.session.execute(
            delete(PublicationAutodeleteLease).where(
                PublicationAutodeleteLease.publication_id == int(handle.publication_id),
                PublicationAutodeleteLease.lease_token == str(handle.lease_token),
            )
        )
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return bool(int(result.rowcount or 0))

    async def current(self, publication_id: int) -> PublicationAutodeleteLease | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        return (
            await self.session.execute(
                select(PublicationAutodeleteLease).where(
                    PublicationAutodeleteLease.publication_id == safe_publication_id
                )
            )
        ).scalar_one_or_none()
