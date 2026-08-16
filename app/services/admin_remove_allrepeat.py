from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.services.canonical_scheduler_admission import CanonicalSchedulerAdmissionService


@dataclass(frozen=True, slots=True)
class AdminRemoveAllRepeatResult:
    removed_pending: int = 0
    disabled_flags: int = 0
    cleared_autodelete: int = 0
    protected_canonical: int = 0


def _apply_legacy_remove_allrepeat_mutations(
    task: PostTask,
    *,
    now: datetime,
) -> tuple[int, int, int]:
    """Apply the historical admin cleanup semantics to one proven legacy-owned row."""

    payload = dict(task.payload or {})
    removed_pending = 0
    disabled_flags = 0
    cleared_autodelete = 0

    if task.status == "pending" and (
        bool(payload.get("repeat_on", False))
        or payload.get("repeat_group_id") is not None
    ):
        task.status = "skipped"
        removed_pending = 1

    if bool(payload.get("repeat_on", False)) or "repeat_seconds" in payload:
        payload["repeat_on"] = False
        payload.pop("repeat_seconds", None)
        task.payload = payload
        disabled_flags = 1

    if (
        "autodelete_at" in payload
        or "autodelete_effective_seconds" in payload
        or "autodelete_seconds" in payload
    ):
        payload.pop("autodelete_at", None)
        payload.pop("autodelete_effective_seconds", None)
        payload.pop("autodelete_seconds", None)
        payload.pop("autodelete_views", None)
        payload["autodeleted"] = True
        payload["autodeleted_at"] = now.isoformat()
        task.payload = payload
        cleared_autodelete = 1

    return removed_pending, disabled_flags, cleared_autodelete


class AdminRemoveAllRepeatService:
    """Run the legacy bulk cleanup only for rows explicitly admitted to legacy ownership."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(self) -> AdminRemoveAllRepeatResult:
        tasks = list(
            (
                await self.session.execute(select(PostTask))
            ).scalars().all()
        )
        admission_service = CanonicalSchedulerAdmissionService(self.session)

        removed_pending = 0
        disabled_flags = 0
        cleared_autodelete = 0
        protected_canonical = 0

        for task in tasks:
            admission = await admission_service.classify(task_id=int(task.id))
            if not admission.legacy_allowed:
                protected_canonical += 1
                continue

            removed, disabled, cleared = _apply_legacy_remove_allrepeat_mutations(
                task,
                now=datetime.now(timezone.utc),
            )
            removed_pending += removed
            disabled_flags += disabled
            cleared_autodelete += cleared

        await self.session.commit()
        return AdminRemoveAllRepeatResult(
            removed_pending=removed_pending,
            disabled_flags=disabled_flags,
            cleared_autodelete=cleared_autodelete,
            protected_canonical=protected_canonical,
        )
