from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.ingestion import SourceIngestionLease


DEFAULT_SOURCE_INGESTION_LEASE_SECONDS = 600


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SourceIngestionLeaseHandle:
    connector_id: int
    lease_token: str
    holder: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SourceIngestionLeaseStatus:
    connector_id: int
    holder: str
    expires_at: datetime


class SourceIngestionLeaseService:
    """Cross-task/process lease for one source connector ingestion run."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def acquire(
        self,
        *,
        connector_id: int,
        holder: str,
        ttl_seconds: int = DEFAULT_SOURCE_INGESTION_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> SourceIngestionLeaseHandle | None:
        current = _utc(now)
        ttl = max(30, min(int(ttl_seconds), 3600))
        expires_at = current + timedelta(seconds=ttl)
        token = uuid.uuid4().hex
        holder_value = str(holder).strip()[:32] or "unknown"

        await self.session.execute(
            delete(SourceIngestionLease).where(
                SourceIngestionLease.connector_id == int(connector_id),
                SourceIngestionLease.expires_at <= current,
            )
        )
        self.session.add(
            SourceIngestionLease(
                connector_id=int(connector_id),
                lease_token=token,
                holder=holder_value,
                expires_at=expires_at,
            )
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return None
        except Exception:
            await self.session.rollback()
            raise
        return SourceIngestionLeaseHandle(
            connector_id=int(connector_id),
            lease_token=token,
            holder=holder_value,
            expires_at=expires_at,
        )

    async def release(self, handle: SourceIngestionLeaseHandle) -> bool:
        result = await self.session.execute(
            delete(SourceIngestionLease).where(
                SourceIngestionLease.connector_id == int(handle.connector_id),
                SourceIngestionLease.lease_token == str(handle.lease_token),
            )
        )
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return bool(int(result.rowcount or 0))

    async def current(self, connector_id: int) -> SourceIngestionLease | None:
        return (
            await self.session.execute(
                select(SourceIngestionLease).where(
                    SourceIngestionLease.connector_id == int(connector_id)
                )
            )
        ).scalar_one_or_none()

    async def active_statuses(
        self,
        connector_ids: list[int] | tuple[int, ...],
        *,
        now: datetime | None = None,
    ) -> dict[int, SourceIngestionLeaseStatus]:
        """Return active lease metadata without exposing lease tokens."""
        ids = sorted({int(value) for value in connector_ids if int(value) > 0})
        if not ids:
            return {}
        current = _utc(now)
        rows = list(
            (
                await self.session.execute(
                    select(SourceIngestionLease).where(
                        SourceIngestionLease.connector_id.in_(ids),
                        SourceIngestionLease.expires_at > current,
                    )
                )
            ).scalars().all()
        )
        return {
            int(row.connector_id): SourceIngestionLeaseStatus(
                connector_id=int(row.connector_id),
                holder=str(row.holder),
                expires_at=_utc(row.expires_at),
            )
            for row in rows
        }
