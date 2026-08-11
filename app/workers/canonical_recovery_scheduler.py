from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domain.models import PostTask
from app.services.canonical_repeat_recovery_shadow import (
    CanonicalRepeatRecoveryShadowCoordinator,
)
from app.workers.canonical_scheduler import Scheduler as CanonicalScheduler


class Scheduler(CanonicalScheduler):
    """Canonical scheduler with a guarded per-occurrence overdue recovery shadow.

    Successful repeat transitions remain implemented by ``CanonicalScheduler``.
    This wrapper observes only ``_skip_overdue_repeat_and_schedule_next``. Boot group
    cleanup stays entirely on the inherited legacy-backed path until it has its own
    canonical group proof.
    """

    def __init__(
        self,
        *args,
        repeat_overdue_recovery_shadow: bool | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._repeat_overdue_recovery_shadow = (
            bool(settings.canonical_repeat_overdue_recovery_shadow_enabled)
            if repeat_overdue_recovery_shadow is None
            else bool(repeat_overdue_recovery_shadow)
        )

    async def _skip_overdue_repeat_and_schedule_next(
        self,
        session: AsyncSession,
        post: PostTask,
        pl: dict,
    ) -> bool:
        parent_recover = super()._skip_overdue_repeat_and_schedule_next
        if not self._repeat_overdue_recovery_shadow:
            return await parent_recover(session, post, pl)

        after = self._boot_time or datetime.now(timezone.utc)

        async def legacy_recover() -> bool:
            return await parent_recover(session, post, pl)

        result = await CanonicalRepeatRecoveryShadowCoordinator(session).run(
            post=post,
            after=after,
            legacy_recover=legacy_recover,
        )
        return bool(result.legacy_recovered)
