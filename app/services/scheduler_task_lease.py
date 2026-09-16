from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.domain.models import PostTask
from app.domain.scheduler import SchedulerTaskLease


DEFAULT_SCHEDULER_LEASE_SECONDS = 180
DEFAULT_RECOVERY_LEASE_SECONDS = 60


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SchedulerTaskLeaseHandle:
    task_id: int
    lease_token: str
    holder: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SchedulerExpiredLeaseRef:
    task_id: int
    lease_token: str
    expires_at: datetime


class SchedulerTaskLeaseService:
    """Atomic PostTask claim plus durable execution liveness lease."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def claim_pending(
        self,
        *,
        task_id: int,
        holder: str,
        ttl_seconds: int = DEFAULT_SCHEDULER_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> SchedulerTaskLeaseHandle | None:
        current = _utc(now)
        ttl = max(30, min(int(ttl_seconds), 3600))
        expires_at = current + timedelta(seconds=ttl)
        token = uuid.uuid4().hex
        holder_value = str(holder).strip()[:64] or "scheduler"

        try:
            # Any existing lease, including an expired one, is a recovery barrier.
            # Never delete it from the normal claim path: an expired processing lease
            # represents an ambiguous Telegram side effect and must be resolved by the
            # fail-closed recovery path before the task can ever become claimable again.
            result = await self.session.execute(
                update(PostTask)
                .where(
                    PostTask.id == int(task_id),
                    PostTask.status == "pending",
                )
                .values(status="processing")
                .execution_options(synchronize_session=False)
            )
            if int(result.rowcount or 0) != 1:
                await self.session.rollback()
                return None

            self.session.add(
                SchedulerTaskLease(
                    task_id=int(task_id),
                    lease_token=token,
                    holder=holder_value,
                    expires_at=expires_at,
                )
            )
            await self.session.commit()
        except IntegrityError:
            # Most commonly an existing active/expired lease. Roll back the status
            # compare-and-set as part of the same transaction.
            await self.session.rollback()
            return None
        except Exception:
            await self.session.rollback()
            raise

        return SchedulerTaskLeaseHandle(
            task_id=int(task_id),
            lease_token=token,
            holder=holder_value,
            expires_at=expires_at,
        )

    async def expired(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[SchedulerExpiredLeaseRef]:
        current = _utc(now)
        rows = (
            await self.session.execute(
                select(
                    SchedulerTaskLease.task_id,
                    SchedulerTaskLease.lease_token,
                    SchedulerTaskLease.expires_at,
                )
                .where(SchedulerTaskLease.expires_at <= current)
                .order_by(
                    SchedulerTaskLease.expires_at.asc(),
                    SchedulerTaskLease.task_id.asc(),
                )
                .limit(max(1, min(int(limit), 500)))
            )
        ).all()
        return [
            SchedulerExpiredLeaseRef(
                task_id=int(row.task_id),
                lease_token=str(row.lease_token),
                expires_at=_utc(row.expires_at),
            )
            for row in rows
        ]

    async def take_expired(
        self,
        reference: SchedulerExpiredLeaseRef,
        *,
        holder: str = "recovery",
        ttl_seconds: int = DEFAULT_RECOVERY_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> SchedulerTaskLeaseHandle | None:
        """Atomically take ownership of one still-expired lease for recovery.

        The token + expiry compare-and-set makes recovery race-safe with a late live
        heartbeat. If the original worker renewed first, rowcount is zero and recovery
        must leave the task untouched.
        """
        current = _utc(now)
        ttl = max(30, min(int(ttl_seconds), 600))
        expires_at = current + timedelta(seconds=ttl)
        token = uuid.uuid4().hex
        holder_value = str(holder).strip()[:64] or "recovery"

        try:
            result = await self.session.execute(
                update(SchedulerTaskLease)
                .where(
                    SchedulerTaskLease.task_id == int(reference.task_id),
                    SchedulerTaskLease.lease_token == str(reference.lease_token),
                    SchedulerTaskLease.expires_at <= current,
                )
                .values(
                    lease_token=token,
                    holder=holder_value,
                    expires_at=expires_at,
                )
                .execution_options(synchronize_session=False)
            )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return None
        except Exception:
            await self.session.rollback()
            raise

        if int(result.rowcount or 0) != 1:
            return None
        return SchedulerTaskLeaseHandle(
            task_id=int(reference.task_id),
            lease_token=token,
            holder=holder_value,
            expires_at=expires_at,
        )

    async def renew(
        self,
        handle: SchedulerTaskLeaseHandle,
        *,
        ttl_seconds: int = DEFAULT_SCHEDULER_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> SchedulerTaskLeaseHandle | None:
        current = _utc(now)
        ttl = max(30, min(int(ttl_seconds), 3600))
        expires_at = current + timedelta(seconds=ttl)
        result = await self.session.execute(
            update(SchedulerTaskLease)
            .where(
                SchedulerTaskLease.task_id == int(handle.task_id),
                SchedulerTaskLease.lease_token == str(handle.lease_token),
                SchedulerTaskLease.expires_at > current,
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
        return SchedulerTaskLeaseHandle(
            task_id=int(handle.task_id),
            lease_token=str(handle.lease_token),
            holder=str(handle.holder),
            expires_at=expires_at,
        )

    async def release(self, handle: SchedulerTaskLeaseHandle) -> bool:
        result = await self.session.execute(
            delete(SchedulerTaskLease).where(
                SchedulerTaskLease.task_id == int(handle.task_id),
                SchedulerTaskLease.lease_token == str(handle.lease_token),
            )
        )
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return bool(int(result.rowcount or 0))

    async def current(self, task_id: int) -> SchedulerTaskLease | None:
        lease = (
            await self.session.execute(
                select(SchedulerTaskLease).where(
                    SchedulerTaskLease.task_id == int(task_id)
                )
            )
        ).scalar_one_or_none()
        if lease is not None:
            # SQLite drops timezone metadata for DateTime(timezone=True). Normalize
            # the loaded value without marking the ORM row dirty so all callers see
            # the same UTC-aware service contract as PostgreSQL callers.
            set_committed_value(lease, "expires_at", _utc(lease.expires_at))
        return lease
