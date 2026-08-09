from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.scheduler import SchedulerTaskLease


DEFAULT_SCHEDULER_LEASE_SECONDS = 180


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
            # Remove only an already-expired orphan from a previous execution. An
            # active row makes the lease INSERT fail and rolls the status CAS back.
            await self.session.execute(
                delete(SchedulerTaskLease).where(
                    SchedulerTaskLease.task_id == int(task_id),
                    SchedulerTaskLease.expires_at <= current,
                )
            )
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
        return (
            await self.session.execute(
                select(SchedulerTaskLease).where(
                    SchedulerTaskLease.task_id == int(task_id)
                )
            )
        ).scalar_one_or_none()
