from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domain.models import PostTask
from app.services.canonical_repeat_boot_recovery_reservation import (
    CanonicalRepeatBootRecoveryReservationService,
)
from app.services.canonical_repeat_boot_recovery_shadow import (
    CanonicalRepeatBootRecoveryShadowCoordinator,
)
from app.services.canonical_repeat_boot_recovery_transport_adapter import (
    CanonicalRepeatBootRecoveryTransportAdapter,
)
from app.services.canonical_repeat_boot_recovery_verifier import (
    CanonicalRepeatBootRecoveryVerifier,
)
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
SAFE_BOOT_RECOVERY_CUTOVER_ERROR = "canonical boot repeat recovery blocked"


class CanonicalRepeatRecoveryCutoverError(RuntimeError):
    """Safe runtime error when opt-in canonical overdue recovery cannot proceed."""


class CanonicalRepeatBootRecoveryCutoverError(RuntimeError):
    """Safe runtime error when opt-in canonical boot-group recovery cannot proceed."""


class Scheduler(CanonicalScheduler):
    """Canonical scheduler with guarded repeat recovery migration stages.

    Successful repeat transitions remain implemented by ``CanonicalScheduler``.
    Per-occurrence overdue recovery and one-time grouped boot cleanup have independent
    default-off shadow/cutover controls so their rollout cannot silently couple.
    """

    def __init__(
        self,
        *args,
        repeat_overdue_recovery_shadow: bool | None = None,
        repeat_overdue_recovery_planning: bool | None = None,
        repeat_boot_recovery_shadow: bool | None = None,
        repeat_boot_recovery_planning: bool | None = None,
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
        self._repeat_boot_recovery_shadow = (
            bool(settings.canonical_repeat_boot_recovery_shadow_enabled)
            if repeat_boot_recovery_shadow is None
            else bool(repeat_boot_recovery_shadow)
        )
        self._repeat_boot_recovery_planning = (
            bool(settings.canonical_repeat_boot_recovery_planning_enabled)
            if repeat_boot_recovery_planning is None
            else bool(repeat_boot_recovery_planning)
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

    @staticmethod
    def _boot_repeat_group_id(post: PostTask) -> int | None:
        payload = dict(post.payload or {})
        if payload.get("repeat_on") is not True:
            return None
        raw_group = payload.get("repeat_group_id")
        try:
            group_id = int(raw_group) if raw_group is not None else int(post.id)
        except (TypeError, ValueError, OverflowError):
            return None
        return group_id if group_id > 0 else None

    def _boot_repeat_groups(
        self,
        items: list[PostTask],
    ) -> list[tuple[int, list[PostTask]]]:
        grouped: dict[int, list[PostTask]] = {}
        order: list[int] = []
        for post in items:
            group_id = self._boot_repeat_group_id(post)
            if group_id is None:
                continue
            if group_id not in grouped:
                grouped[group_id] = []
                order.append(group_id)
            grouped[group_id].append(post)
        return [(group_id, grouped[group_id]) for group_id in order]

    def _canonical_boot_group_candidate(
        self,
        posts: list[PostTask],
        *,
        after: datetime,
    ) -> bool:
        if not posts:
            return False
        expected_group = self._boot_repeat_group_id(posts[0])
        if expected_group is None:
            return False
        for post in posts:
            if self._boot_repeat_group_id(post) != expected_group:
                return False
            payload = dict(post.payload or {})
            try:
                repeat_seconds = int(payload.get("repeat_seconds") or 0)
            except (TypeError, ValueError, OverflowError):
                return False
            if (
                repeat_seconds <= 0
                or post.scheduled_at is None
                or self._as_utc(post.scheduled_at) > after
            ):
                return False
        return True

    async def _canonical_boot_recover_group(
        self,
        session: AsyncSession,
        posts: list[PostTask],
        *,
        after: datetime,
    ) -> None:
        group_id = self._boot_repeat_group_id(posts[0]) if posts else None
        try:
            if group_id is None:
                raise CanonicalRepeatBootRecoveryCutoverError(
                    SAFE_BOOT_RECOVERY_CUTOVER_ERROR
                )

            source_publication_ids: list[int] = []
            for post in posts:
                publication_id = await self._source_publication_id(session, post)
                if publication_id is None:
                    raise CanonicalRepeatBootRecoveryCutoverError(
                        SAFE_BOOT_RECOVERY_CUTOVER_ERROR
                    )
                source_publication_ids.append(publication_id)

            reservation = await CanonicalRepeatBootRecoveryReservationService(
                session
            ).reserve_group(
                source_publication_ids,
                after=after,
            )
            if reservation.outcome not in {"reserved", "already_reserved"}:
                logger.warning(
                    "Canonical boot recovery reservation blocked group_id={} sources={} "
                    "outcome={}",
                    group_id,
                    len(source_publication_ids),
                    reservation.outcome,
                )
                raise CanonicalRepeatBootRecoveryCutoverError(
                    SAFE_BOOT_RECOVERY_CUTOVER_ERROR
                )

            materialized = await CanonicalRepeatBootRecoveryTransportAdapter(
                session
            ).materialize(source_publication_ids)
            if materialized.outcome not in {"created", "existing"}:
                logger.warning(
                    "Canonical boot recovery materialization blocked group_id={} sources={} "
                    "outcome={}",
                    group_id,
                    len(source_publication_ids),
                    materialized.outcome,
                )
                raise CanonicalRepeatBootRecoveryCutoverError(
                    SAFE_BOOT_RECOVERY_CUTOVER_ERROR
                )

            verification = await CanonicalRepeatBootRecoveryVerifier(session).verify(
                source_publication_ids
            )
            if verification.outcome != "matched":
                # Materialization already committed every source skip and the single
                # future child. Never route this group back through the legacy creator.
                logger.error(
                    "Canonical boot recovery post-commit verification mismatch "
                    "group_id={} sources={} outcome={}",
                    group_id,
                    len(source_publication_ids),
                    verification.outcome,
                )
            else:
                logger.debug(
                    "Canonical boot recovery cutover matched group_id={} sources={} "
                    "successor_publication_id={}",
                    group_id,
                    len(source_publication_ids),
                    verification.successor_publication_id,
                )
        except asyncio.CancelledError:
            raise
        except CanonicalRepeatBootRecoveryCutoverError:
            raise
        except Exception as exc:
            logger.warning(
                "Canonical boot recovery cutover failed group_id={} type={}",
                group_id,
                type(exc).__name__,
            )
            raise CanonicalRepeatBootRecoveryCutoverError(
                SAFE_BOOT_RECOVERY_CUTOVER_ERROR
            ) from None

    async def _boot_cleanup_repeats(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> list[PostTask]:
        parent_cleanup = super()._boot_cleanup_repeats
        if self._boot_cleanup_done or self._boot_time is None or not items:
            return await parent_cleanup(session, items)

        if self._repeat_boot_recovery_planning:
            selected = list(items)
            canonical_task_ids: set[int] = set()
            for _group_id, posts in self._boot_repeat_groups(selected):
                if not self._canonical_boot_group_candidate(
                    posts,
                    after=self._boot_time,
                ):
                    continue
                # A candidate group is all-or-nothing. Once its canonical attempt starts,
                # a reservation/materialization conflict never falls back to the legacy
                # creator for any member of that same group.
                await self._canonical_boot_recover_group(
                    session,
                    posts,
                    after=self._boot_time,
                )
                canonical_task_ids.update(int(post.id) for post in posts)

            legacy_items = [
                post for post in items if int(post.id) not in canonical_task_ids
            ]
            # Always invoke the inherited cleanup once, including an empty list, so the
            # scheduler preserves the legacy one-time `_boot_cleanup_done` transition.
            return await parent_cleanup(session, legacy_items)

        if not self._repeat_boot_recovery_shadow:
            return await parent_cleanup(session, items)

        selected = list(items)

        async def legacy_cleanup() -> list[PostTask]:
            return await parent_cleanup(session, items)

        result = await CanonicalRepeatBootRecoveryShadowCoordinator(session).run(
            items=selected,
            after=self._boot_time,
            legacy_cleanup=legacy_cleanup,
        )
        return list(result.remaining)
