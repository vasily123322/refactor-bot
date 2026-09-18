from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.legacy_time_views_delete_action import LegacyTimeViewsDeleteAction
from app.domain.models import PostTask
from app.domain.publishing.models import (
    CanonicalRuntimeSafetyAudit,
    Publication,
    ScheduleEntry,
)
from app.domain.scheduler import SchedulerTaskLease


_ACTIVE_TASK_STATUSES = {"pending", "processing"}
_ACTIVE_PUBLICATION_STATUSES = {"queued", "sending"}


@dataclass(frozen=True, slots=True)
class LegacyRuntimeDrainBatch:
    scanned: int
    archived: int
    unlinked: int
    retained_active: int
    next_cursor: int
    done: bool


def legacy_runtime_source_fingerprint(task_id: int) -> str:
    return sha256(f"legacy-post-task:{int(task_id)}".encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class LegacyRuntimeDrainService:
    """Archive legacy runtime evidence and unlink only terminal, lease-free rows.

    The audit is deliberately independent of the PostTask schema. Reserved/unknown
    destructive actions and UNKNOWN_DELIVERY_ERROR are copied before a link is cleared,
    so later PostTask schema removal cannot create replay permission.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def drain_batch(
        self,
        *,
        after_task_id: int = 0,
        limit: int = 100,
    ) -> LegacyRuntimeDrainBatch:
        safe_after = max(0, int(after_task_id))
        safe_limit = max(1, min(int(limit), 500))
        tasks = list(
            (
                await self.session.execute(
                    select(PostTask)
                    .where(PostTask.id > safe_after)
                    .order_by(PostTask.id.asc())
                    .limit(safe_limit)
                )
            ).scalars().all()
        )
        archived = 0
        unlinked = 0
        retained_active = 0
        next_cursor = safe_after

        for task in tasks:
            task_id = int(task.id)
            next_cursor = task_id
            publication = (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.legacy_post_task_id == task_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            lease = await self.session.get(SchedulerTaskLease, task_id)
            destructive = (
                await self.session.execute(
                    select(LegacyTimeViewsDeleteAction)
                    .where(LegacyTimeViewsDeleteAction.post_task_id == task_id)
                    .limit(1)
                )
            ).scalar_one_or_none()

            fingerprint = legacy_runtime_source_fingerprint(task_id)
            payload = deepcopy(dict(task.payload or {}))
            destructive_evidence: dict[str, Any] | None = None
            if destructive is not None:
                destructive_evidence = {
                    "state": str(destructive.state),
                    "chat_id": int(destructive.chat_id),
                    "message_ids": list(destructive.message_ids or []),
                    "target_fingerprint": str(destructive.target_fingerprint),
                    "reservation_token": str(destructive.reservation_token),
                    "reserved_at": _iso(destructive.reserved_at),
                    "finalized_at": _iso(destructive.finalized_at),
                    "automatic_replay_forbidden": str(destructive.state)
                    in {"reserved", "unknown", "succeeded"},
                }

            unknown_delivery = str(task.error or "") == "UNKNOWN_DELIVERY_ERROR"
            task_active = str(task.status or "") in _ACTIVE_TASK_STATUSES
            publication_active = (
                publication is not None
                and str(publication.status or "") in _ACTIVE_PUBLICATION_STATUSES
            )
            has_live_lease = lease is not None
            state = (
                "active"
                if task_active or publication_active or has_live_lease
                else (
                    "terminal_no_replay"
                    if unknown_delivery
                    or (
                        destructive_evidence is not None
                        and destructive_evidence["automatic_replay_forbidden"]
                    )
                    else "terminal_archived"
                )
            )
            evidence = {
                "version": 1,
                "legacy_transport": {
                    "status": str(task.status or ""),
                    "channel_id": int(task.channel_id),
                    "scheduled_at": _iso(task.scheduled_at),
                    "dedupe_key": task.dedupe_key,
                    "error": task.error,
                    "payload": payload,
                    "unknown_delivery_no_replay": unknown_delivery,
                },
                "destructive_action": destructive_evidence,
                "publication_id": int(publication.id) if publication is not None else None,
                "scheduler_lease_present": has_live_lease,
            }

            audit = (
                await self.session.execute(
                    select(CanonicalRuntimeSafetyAudit)
                    .where(
                        CanonicalRuntimeSafetyAudit.source_fingerprint == fingerprint
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if audit is None:
                audit = CanonicalRuntimeSafetyAudit(
                    publication_id=(
                        int(publication.id) if publication is not None else None
                    ),
                    source_fingerprint=fingerprint,
                    state=state,
                    evidence=evidence,
                )
                self.session.add(audit)
            else:
                audit.publication_id = (
                    int(publication.id) if publication is not None else audit.publication_id
                )
                audit.state = state
                audit.evidence = evidence
            archived += 1

            if publication is None:
                continue
            if task_active or publication_active or has_live_lease:
                retained_active += 1
                continue

            publication_meta = deepcopy(dict(publication.meta or {}))
            publication_meta["legacy_runtime_audit_fingerprint"] = fingerprint
            publication_meta.setdefault("legacy_post_task_callback_id", task_id)
            publication.meta = publication_meta

            if publication.schedule_entry_id is not None:
                schedule = await self.session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id),
                )
                if schedule is not None:
                    schedule.meta = {
                        **deepcopy(dict(schedule.meta or {})),
                        "legacy_runtime_audit_fingerprint": fingerprint,
                    }

            publication.legacy_post_task_id = None
            unlinked += 1

        await self.session.commit()
        done = len(tasks) < safe_limit
        return LegacyRuntimeDrainBatch(
            scanned=len(tasks),
            archived=archived,
            unlinked=unlinked,
            retained_active=retained_active,
            next_cursor=(0 if done else next_cursor),
            done=done,
        )
