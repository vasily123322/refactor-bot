from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domain.models import PostTask
from app.services.canonical_repeat_recovery_reservation import (
    CanonicalRepeatRecoveryReservationService,
)
from app.services.canonical_repeat_recovery_shadow import (
    CanonicalRepeatRecoveryShadowCoordinator,
)
from app.services.canonical_repeat_recovery_transport_adapter import (
    CanonicalRepeatRecoveryTransportAdapter,
)
from app.services.canonical_repeat_recovery_verifier import (
    CanonicalRepeatRecoveryVerifier,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_scheduler import Scheduler as CanonicalScheduler


SAFE_OVERDUE_RECOVERY_CUTOVER_ERROR = "canonical overdue repeat recovery blocked"


class CanonicalRepeatRecoveryCutoverError(RuntimeError):
    """Safe runtime error when opt-in canonical overdue recovery cannot proceed."""


class Scheduler(CanonicalScheduler):
    """Canonical scheduler with guarded per-occurrence overdue recovery migration.

    Successful repeat transitions remain implemented by ``CanonicalScheduler``.
    This wrapper owns only ``_skip_overdue_repeat_and_schedule_next``. Boot group
    cleanup stays entirely on the inherited legacy-backed path until it has its own
    canonical group proof.
    """

    def __init__(
        self,
        *args,
        repeat_overdue_recovery_shadow: bool | None = None,
        repeat_overdue_recovery_planning: bool | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._repeat_overdue_recovery_shadow = (
            bool(settings.canonical_repeat_overdue_recovery_shadow_enabled)
            if repeat_overdue_recovery_shadow is None
            else bool(repeat_overdue_recovery_shadow)
        )
        self._repeat_overdue_recovery_planning = (
            bool(settings.canonical_repeat_overdue_recovery_planning_enabled)
            if repeat_overdue_recovery_planning is None
            else bool(repeat_overdue_recovery_planning)
        )

    async def _source_publication_id(
        self,
        session: AsyncSession,
        post: PostTask,
    ) -> int | None:
        publication = await LegacyPublicationBridge(session).reconcile_task(post)
        if publication is None:
            publication = await mirror_legacy_post_task(session, post)
        return int(publication.id) if publication is not None else None

    def _canonical_recovery_candidate(
        self,
        post: PostTask,
        pl: dict,
        *,
        after: datetime,
    ) -> bool:
        if not bool(pl.get("repeat_on", False)):
            return False
        try:
            repeat_seconds = int(pl.get("repeat_seconds") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if repeat_seconds <= 0 or post.scheduled_at is None:
            return False
        return self._as_utc(post.scheduled_at) <= after

    async def _canonical_recover_overdue_repeat(
        self,
        session: AsyncSession,
        post: PostTask,
        *,
        after: datetime,
    ) -> bool:
        try:
            source_publication_id = await self._source_publication_id(session, post)
            if source_publication_id is None:
                raise CanonicalRepeatRecoveryCutoverError(
                    SAFE_OVERDUE_RECOVERY_CUTOVER_ERROR
                )

            reservation = await CanonicalRepeatRecoveryReservationService(
                session
            ).reserve_recovery(
                source_publication_id,
                after=after,
            )
            if reservation.outcome not in {"reserved", "already_reserved"}:
                logger.warning(
                    "Canonical overdue recovery reservation blocked post_id={} "
                    "source_publication_id={} outcome={}",
                    int(post.id),
                    source_publication_id,
                    reservation.outcome,
                )
                raise CanonicalRepeatRecoveryCutoverError(
                    SAFE_OVERDUE_RECOVERY_CUTOVER_ERROR
                )

            materialized = await CanonicalRepeatRecoveryTransportAdapter(
                session
            ).materialize(source_publication_id)
            if materialized.outcome not in {"created", "existing"}:
                logger.warning(
                    "Canonical overdue recovery materialization blocked post_id={} "
                    "source_publication_id={} outcome={}",
                    int(post.id),
                    source_publication_id,
                    materialized.outcome,
                )
                raise CanonicalRepeatRecoveryCutoverError(
                    SAFE_OVERDUE_RECOVERY_CUTOVER_ERROR
                )

            verification = await CanonicalRepeatRecoveryVerifier(session).verify(
                source_publication_id
            )
            if verification.outcome != "matched":
                # Materialization has already committed the source skip + child. Do not
                # raise into the legacy per-item error handler, which would mutate the
                # source transport from skipped to failed. Block stale delivery by
                # returning handled=True and surface the mismatch operationally.
                logger.error(
                    "Canonical overdue recovery post-commit verification mismatch "
                    "post_id={} source_publication_id={} outcome={}",
                    int(post.id),
                    source_publication_id,
                    verification.outcome,
                )
            else:
                logger.debug(
                    "Canonical overdue recovery cutover matched post_id={} "
                    "source_publication_id={} successor_publication_id={}",
                    int(post.id),
                    source_publication_id,
                    verification.successor_publication_id,
                )
            return True
        except asyncio.CancelledError:
            raise
        except CanonicalRepeatRecoveryCutoverError:
            raise
        except Exception as exc:
            logger.warning(
                "Canonical overdue recovery cutover failed post_id={} type={}",
                int(post.id),
                type(exc).__name__,
            )
            raise CanonicalRepeatRecoveryCutoverError(
                SAFE_OVERDUE_RECOVERY_CUTOVER_ERROR
            ) from None

    async def _skip_overdue_repeat_and_schedule_next(
        self,
        session: AsyncSession,
        post: PostTask,
        pl: dict,
    ) -> bool:
        parent_recover = super()._skip_overdue_repeat_and_schedule_next
        after = self._boot_time or datetime.now(timezone.utc)

        if self._repeat_overdue_recovery_planning:
            if not self._canonical_recovery_candidate(post, pl, after=after):
                return await parent_recover(session, post, pl)
            # Once the opt-in canonical attempt begins, never fall back to the legacy
            # child creator in the same transition. A pre-commit conflict raises the
            # safe static error so the stale overdue occurrence is not delivered.
            return await self._canonical_recover_overdue_repeat(
                session,
                post,
                after=after,
            )

        if not self._repeat_overdue_recovery_shadow:
            return await parent_recover(session, post, pl)

        async def legacy_recover() -> bool:
            return await parent_recover(session, post, pl)

        result = await CanonicalRepeatRecoveryShadowCoordinator(session).run(
            post=post,
            after=after,
            legacy_recover=legacy_recover,
        )
        return bool(result.legacy_recovered)
