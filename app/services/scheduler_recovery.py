from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_task_lease import (
    SchedulerExpiredLeaseRef,
    SchedulerTaskLeaseService,
)
from app.services.telegram_results import normalize_telegram_message_ids


UNKNOWN_DELIVERY_ERROR = (
    "scheduler execution lease expired with unknown delivery outcome; "
    "automatic retry disabled"
)
_TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "skipped", "cancelled"})


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SchedulerRecoveryTick:
    selected: int = 0
    taken_over: int = 0
    confirmed_published: int = 0
    failed_unknown: int = 0
    terminal_cleaned: int = 0
    orphaned: int = 0
    contention: int = 0
    failures: int = 0


class SchedulerTaskRecoveryService:
    """Resolve expired execution leases without ever retrying Telegram delivery."""

    def __init__(self, session_factory: Callable[[], AsyncSession]):
        self.session_factory = session_factory

    async def _project_terminal(self, session: AsyncSession, task: PostTask) -> None:
        try:
            await LegacyPublicationBridge(session).reconcile_task(task)
        except Exception as exc:
            # The PostTask terminal state is already durable. PublicationReconciler
            # remains a backfill path, so projection failure must not reopen delivery.
            await session.rollback()
            logger.warning(
                "Scheduler recovery: publication projection failed type={}",
                type(exc).__name__,
            )

    async def _recover_reference(
        self,
        reference: SchedulerExpiredLeaseRef,
        *,
        now: datetime,
    ) -> str:
        async with self.session_factory() as session:
            leases = SchedulerTaskLeaseService(session)
            handle = await leases.take_expired(reference, now=now)
            if handle is None:
                return "contention"

            task = await session.get(PostTask, int(reference.task_id))
            if task is None:
                await leases.release(handle)
                return "orphaned"

            task_status = str(task.status or "")
            if task_status in _TERMINAL_TASK_STATUSES:
                await self._project_terminal(session, task)
                await leases.release(handle)
                return "terminal_cleaned"

            payload = dict(task.payload or {})
            result_ids = normalize_telegram_message_ids(payload.get("result_ids"))
            if result_ids:
                # Scheduler persists result_ids immediately after send_now succeeds,
                # before pin/forward/owner notification/final done. That is durable
                # evidence that the primary Telegram delivery happened; do not resend.
                payload["result_ids"] = result_ids
                task.payload = payload
                task.status = "done"
                task.error = None
                outcome = "confirmed_published"
            else:
                # No durable delivery evidence. The outcome may be pre-send failure or
                # a send whose response was lost. Both are unsafe to retry blindly.
                task.status = "failed"
                task.error = UNKNOWN_DELIVERY_ERROR
                outcome = "failed_unknown"

            try:
                await session.commit()
            except Exception:
                await session.rollback()
                # Keep the recovery-owned lease until its short expiry. A later tick
                # can safely try again without reopening normal scheduler claim.
                raise

            await self._project_terminal(session, task)
            await leases.release(handle)
            return outcome

    async def run_once(
        self,
        *,
        batch_size: int = 100,
        now: datetime | None = None,
    ) -> SchedulerRecoveryTick:
        current = _utc(now)
        limit = max(1, min(int(batch_size), 500))
        async with self.session_factory() as session:
            references = await SchedulerTaskLeaseService(session).expired(
                limit=limit,
                now=current,
            )

        counts = {
            "taken_over": 0,
            "confirmed_published": 0,
            "failed_unknown": 0,
            "terminal_cleaned": 0,
            "orphaned": 0,
            "contention": 0,
            "failures": 0,
        }
        for reference in references:
            try:
                outcome = await self._recover_reference(reference, now=current)
            except Exception as exc:
                counts["failures"] += 1
                logger.warning(
                    "Scheduler recovery: candidate failed type={}",
                    type(exc).__name__,
                )
                continue

            if outcome != "contention":
                counts["taken_over"] += 1
            counts[outcome] += 1

        return SchedulerRecoveryTick(
            selected=len(references),
            **counts,
        )
