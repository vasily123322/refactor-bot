from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_primary_started,
    canonical_publication_delivery_repeat_started,
    canonical_publication_delivery_repeat_time_forward_started,
    canonical_publication_delivery_repeat_time_pin_forward_started,
    canonical_publication_delivery_repeat_time_pin_started,
    canonical_publication_delivery_repeat_time_started,
    canonical_publication_delivery_repeat_views_forward_started,
    canonical_publication_delivery_repeat_views_pin_forward_started,
    canonical_publication_delivery_repeat_views_pin_started,
    canonical_publication_delivery_repeat_views_started,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_linked_repeat_parity import (
    CanonicalPublicationLinkedRepeatParityService,
)
from app.services.canonical_publication_linked_repeat_time_forward_parity import (
    CanonicalPublicationLinkedRepeatTimeForwardParityService,
)
from app.services.canonical_publication_linked_repeat_time_pin_forward_parity import (
    CanonicalPublicationLinkedRepeatTimePinForwardParityService,
)
from app.services.canonical_publication_linked_repeat_time_pin_parity import (
    CanonicalPublicationLinkedRepeatTimePinParityService,
)
from app.services.canonical_publication_nonrepeat_scheduler_proof import (
    CanonicalPublicationNonrepeatSchedulerProofService,
)
from app.workers.canonical_recovery_scheduler import Scheduler as RecoveryScheduler
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


class Scheduler(RecoveryScheduler):
    """Recovery scheduler plus provider-free canonical repeat continuation lifecycle.

    Exact fixed-delay linked repeats may yield before the inherited legacy lease when a
    successfully started canonical repeat primary is live. Established non-destructive
    profiles, proven repeat+time compositions and the explicitly enabled repeat+views
    lattice remain eligible. Every views composition requires its dedicated started fact;
    the combined pin+forward profile additionally requires both sibling facts. Scheduler
    admission performs no cutover mutation; canonical primary owns atomic handoff/claim.
    Time+views remains intentionally on the inherited legacy path.
    """

    def __init__(
        self,
        *args,
        repeat_continuation_enabled: bool | None = None,
        continuation_worker_factory: Callable[..., CanonicalRepeatContinuationWorker] = (
            CanonicalRepeatContinuationWorker
        ),
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._repeat_continuation_enabled = (
            bool(settings.canonical_repeat_successful_planning_enabled)
            if repeat_continuation_enabled is None
            else bool(repeat_continuation_enabled)
        )
        self._continuation_worker_factory = continuation_worker_factory
        self._repeat_continuation_worker: CanonicalRepeatContinuationWorker | None = None

    @property
    def repeat_continuation_available(self) -> bool:
        return self._repeat_continuation_worker is not None

    def _continuation_session_factory(
        self,
    ) -> async_sessionmaker[AsyncSession] | None:
        factory = getattr(self, "session_factory", None)
        return factory if factory is not None else None

    async def _yield_proven_repeat_to_canonical_primary(
        self,
        session: AsyncSession,
        *,
        task_id: int,
    ) -> bool:
        """Yield one exact proven repeat profile without mutating authority."""

        if not canonical_publication_delivery_repeat_started():
            return False

        publications = list(
            (
                await session.execute(
                    select(Publication)
                    .where(Publication.legacy_post_task_id == int(task_id))
                    .limit(2)
                )
            ).scalars().all()
        )
        if len(publications) != 1:
            return False
        publication = publications[0]
        if publication.schedule_entry_id is None:
            return False

        task = await session.get(PostTask, int(task_id), populate_existing=True)
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id),
            populate_existing=True,
        )
        if task is None or schedule is None:
            return False

        plan = await CanonicalPublicationDeliveryPlanner(session).plan(int(publication.id))
        if plan is None:
            return False
        try:
            runtime_options = plan.runtime_options()
        except (TypeError, ValueError):
            return False
        if not isinstance(runtime_options, dict):
            return False

        runtime_keys = set(runtime_options)
        forward_value = runtime_options.get("forward_to")
        preliminary_time_pin_forward_profile = (
            runtime_keys.issubset(
                {
                    "silent",
                    "pin_on",
                    "forward_to",
                    "autodelete_seconds",
                    "autodelete_report",
                }
            )
            and runtime_options.get("pin_on") is True
            and "forward_to" in runtime_options
            and isinstance(forward_value, list)
            and bool(forward_value)
            and "autodelete_seconds" in runtime_options
        )
        preliminary_time_pin_profile = (
            runtime_keys.issubset(
                {"silent", "pin_on", "autodelete_seconds", "autodelete_report"}
            )
            and runtime_options.get("pin_on") is True
            and "autodelete_seconds" in runtime_options
        )
        preliminary_time_forward_profile = (
            runtime_keys.issubset(
                {"silent", "forward_to", "autodelete_seconds", "autodelete_report"}
            )
            and "forward_to" in runtime_options
            and isinstance(forward_value, list)
            and bool(forward_value)
            and "autodelete_seconds" in runtime_options
        )
        if preliminary_time_pin_forward_profile:
            parity_service = CanonicalPublicationLinkedRepeatTimePinForwardParityService()
        elif preliminary_time_pin_profile:
            parity_service = CanonicalPublicationLinkedRepeatTimePinParityService()
        elif preliminary_time_forward_profile:
            parity_service = CanonicalPublicationLinkedRepeatTimeForwardParityService()
        else:
            parity_service = CanonicalPublicationLinkedRepeatParityService()
        proof = parity_service.prove(
            task=task,
            publication=publication,
            schedule=schedule,
            plan=plan,
        )
        if proof is None:
            return False

        plain_profile = (
            runtime_keys.issubset({"silent"})
            and not proof.pin_on
            and not proof.forward_channel_ids
        )
        pin_profile = (
            runtime_keys.issubset({"silent", "pin_on"})
            and runtime_options.get("pin_on") is True
            and proof.pin_on
            and not proof.forward_channel_ids
        )
        forward_profile = (
            runtime_keys.issubset({"silent", "forward_to"})
            and "forward_to" in runtime_options
            and isinstance(forward_value, list)
            and bool(forward_value)
            and not proof.pin_on
            and bool(proof.forward_channel_ids)
        )
        pin_forward_profile = (
            runtime_keys.issubset({"silent", "pin_on", "forward_to"})
            and runtime_options.get("pin_on") is True
            and "forward_to" in runtime_options
            and isinstance(forward_value, list)
            and bool(forward_value)
            and proof.pin_on
            and bool(proof.forward_channel_ids)
        )
        time_value = runtime_options.get("autodelete_seconds")
        positive_exact_time = (
            "autodelete_seconds" in runtime_options
            and isinstance(time_value, int)
            and not isinstance(time_value, bool)
            and time_value > 0
            and proof.time_autodelete_seconds == time_value
        )
        time_profile = (
            runtime_keys.issubset(
                {"silent", "autodelete_seconds", "autodelete_report"}
            )
            and positive_exact_time
            and not proof.pin_on
            and not proof.forward_channel_ids
            and proof.views_autodelete_threshold is None
            and not proof.autodelete_report
            and not proof.views_pin_forward_composed
            and runtime_options.get("autodelete_report") in (None, False)
        )
        time_pin_profile = (
            preliminary_time_pin_profile
            and positive_exact_time
            and proof.pin_on
            and not proof.forward_channel_ids
            and proof.views_autodelete_threshold is None
            and not proof.autodelete_report
            and not proof.views_pin_forward_composed
            and runtime_options.get("autodelete_report") in (None, False)
        )
        time_forward_profile = (
            preliminary_time_forward_profile
            and positive_exact_time
            and not proof.pin_on
            and bool(proof.forward_channel_ids)
            and tuple(proof.forward_channel_ids) == tuple(forward_value)
            and proof.views_autodelete_threshold is None
            and not proof.autodelete_report
            and not proof.views_pin_forward_composed
            and runtime_options.get("autodelete_report") in (None, False)
        )
        time_pin_forward_profile = (
            preliminary_time_pin_forward_profile
            and positive_exact_time
            and proof.pin_on
            and bool(proof.forward_channel_ids)
            and tuple(proof.forward_channel_ids) == tuple(forward_value)
            and proof.views_autodelete_threshold is None
            and not proof.autodelete_report
            and not proof.views_pin_forward_composed
            and runtime_options.get("autodelete_report") in (None, False)
        )
        views_value = runtime_options.get("autodelete_views")
        positive_exact_views = (
            "autodelete_views" in runtime_options
            and isinstance(views_value, int)
            and not isinstance(views_value, bool)
            and views_value > 0
            and proof.views_autodelete_threshold == views_value
        )
        views_profile = (
            runtime_keys.issubset({"silent", "autodelete_views", "autodelete_report"})
            and positive_exact_views
            and proof.time_autodelete_seconds is None
            and not proof.pin_on
            and not proof.forward_channel_ids
            and not proof.autodelete_report
            and not proof.views_pin_forward_composed
            and runtime_options.get("autodelete_report") in (None, False)
        )
        views_pin_profile = (
            runtime_keys.issubset(
                {"silent", "pin_on", "autodelete_views", "autodelete_report"}
            )
            and runtime_options.get("pin_on") is True
            and positive_exact_views
            and proof.time_autodelete_seconds is None
            and proof.pin_on
            and not proof.forward_channel_ids
            and not proof.autodelete_report
            and not proof.views_pin_forward_composed
            and runtime_options.get("autodelete_report") in (None, False)
        )
        views_forward_profile = (
            runtime_keys.issubset(
                {"silent", "forward_to", "autodelete_views", "autodelete_report"}
            )
            and "forward_to" in runtime_options
            and isinstance(forward_value, list)
            and bool(forward_value)
            and positive_exact_views
            and proof.time_autodelete_seconds is None
            and not proof.pin_on
            and bool(proof.forward_channel_ids)
            and tuple(proof.forward_channel_ids) == tuple(forward_value)
            and not proof.autodelete_report
            and not proof.views_pin_forward_composed
            and runtime_options.get("autodelete_report") in (None, False)
        )
        views_pin_forward_profile = (
            runtime_keys.issubset(
                {
                    "silent",
                    "pin_on",
                    "forward_to",
                    "autodelete_views",
                    "autodelete_report",
                }
            )
            and runtime_options.get("pin_on") is True
            and "forward_to" in runtime_options
            and isinstance(forward_value, list)
            and bool(forward_value)
            and positive_exact_views
            and proof.time_autodelete_seconds is None
            and proof.pin_on
            and bool(proof.forward_channel_ids)
            and tuple(proof.forward_channel_ids) == tuple(forward_value)
            and not proof.autodelete_report
            and proof.views_pin_forward_composed
            and runtime_options.get("autodelete_report") in (None, False)
        )
        if time_profile and not canonical_publication_delivery_repeat_time_started():
            return False
        if time_pin_profile and not canonical_publication_delivery_repeat_time_pin_started():
            return False
        if time_forward_profile and not canonical_publication_delivery_repeat_time_forward_started():
            return False
        if (
            time_pin_forward_profile
            and not canonical_publication_delivery_repeat_time_pin_forward_started()
        ):
            return False
        if views_profile and not canonical_publication_delivery_repeat_views_started():
            return False
        if views_pin_profile and not canonical_publication_delivery_repeat_views_pin_started():
            return False
        if (
            views_forward_profile
            and not canonical_publication_delivery_repeat_views_forward_started()
        ):
            return False
        if (
            views_pin_forward_profile
            and not canonical_publication_delivery_repeat_views_pin_forward_started()
        ):
            return False
        if not (
            plain_profile
            or pin_profile
            or forward_profile
            or pin_forward_profile
            or time_profile
            or time_pin_profile
            or time_forward_profile
            or time_pin_forward_profile
            or views_profile
            or views_pin_profile
            or views_forward_profile
            or views_pin_forward_profile
        ):
            return False
        if (
            (
                proof.time_autodelete_seconds is not None
                and not (
                    time_profile
                    or time_pin_profile
                    or time_forward_profile
                    or time_pin_forward_profile
                )
            )
            or (
                proof.views_autodelete_threshold is not None
                and not (
                    views_profile
                    or views_pin_profile
                    or views_forward_profile
                    or views_pin_forward_profile
                )
            )
            or proof.autodelete_report
            or (proof.views_pin_forward_composed and not views_pin_forward_profile)
        ):
            return False

        logger.info(
            "Scheduler: yielding exact repeat to canonical primary post_id={} publication_id={} repeat_group_id={} pin_on={} forward_targets={} time_autodelete_seconds={} views_autodelete_threshold={}",
            int(task_id),
            int(publication.id),
            int(proof.repeat_group_id),
            bool(proof.pin_on),
            len(proof.forward_channel_ids),
            proof.time_autodelete_seconds,
            proof.views_autodelete_threshold,
        )
        return True

    async def _yield_proven_nonrepeat_to_canonical_primary(
        self,
        session: AsyncSession,
        *,
        task_id: int,
    ) -> bool:
        """Yield one exact non-repeat profile without mutating cutover authority."""

        proof = await CanonicalPublicationNonrepeatSchedulerProofService(
            session
        ).prove_plain(task_id=int(task_id))
        if proof is None:
            return False

        logger.info(
            "Scheduler: yielding exact nonrepeat to canonical primary post_id={} publication_id={} profile={}",
            int(task_id),
            int(proof.publication_id),
            proof.profile,
        )
        return True

    async def _legacy_repeat_boot_mutation_protected(
        self,
        session: AsyncSession,
        *,
        task_id: int,
    ) -> bool:
        """Return whether a retired repeat profile must bypass legacy boot mutation."""

        if not canonical_publication_delivery_repeat_started():
            return False
        try:
            return await self._yield_proven_repeat_to_canonical_primary(
                session,
                task_id=int(task_id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await session.rollback()
            logger.warning(
                "Scheduler: repeat boot ownership proof failed post_id={} type={}",
                int(task_id),
                type(exc).__name__,
            )
            return False

    async def _boot_cleanup_repeats(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> list[PostTask]:
        """Exclude canonical-owned repeat occurrences from legacy boot creators."""

        if not items or not canonical_publication_delivery_repeat_started():
            return await super()._boot_cleanup_repeats(session, items)

        protected_ids: set[int] = set()
        legacy_items: list[PostTask] = []
        for post in items:
            task_id = int(post.id)
            if await self._legacy_repeat_boot_mutation_protected(
                session,
                task_id=task_id,
            ):
                protected_ids.add(task_id)
            else:
                current = await session.get(PostTask, task_id, populate_existing=True)
                if current is not None:
                    legacy_items.append(current)

        remaining_legacy = await super()._boot_cleanup_repeats(session, legacy_items)
        remaining_ids = protected_ids | {int(post.id) for post in remaining_legacy}
        result: list[PostTask] = []
        for post in items:
            if int(post.id) not in remaining_ids:
                continue
            current = await session.get(PostTask, int(post.id), populate_existing=True)
            if current is not None:
                result.append(current)
        return result

    async def _prevent_repeat_overflow(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        """Keep legacy overflow cleanup away from canonical-owned repeat occurrences."""

        if not items or not canonical_publication_delivery_repeat_started():
            await super()._prevent_repeat_overflow(session, items)
            return

        res = await session.execute(
            select(PostTask)
            .where(PostTask.status == "pending")
            .order_by(PostTask.scheduled_at.asc())
            .limit(500)
        )
        groups: dict[int, list[PostTask]] = {}
        for post in list(res.scalars().all()):
            payload = dict(post.payload or {})
            if not bool(payload.get("repeat_on", False)):
                continue
            if await self._legacy_repeat_boot_mutation_protected(
                session,
                task_id=int(post.id),
            ):
                continue
            group_id = int(payload.get("repeat_group_id") or int(post.id))
            groups.setdefault(group_id, []).append(post)

        for _group_id, posts in groups.items():
            limit = max(1, int(getattr(settings, "repeat_overflow_limit", 2)))
            if len(posts) <= limit:
                continue
            ordered = sorted(
                [post for post in posts if post.scheduled_at is not None],
                key=lambda post: post.scheduled_at,
            )
            keep = None
            for post in ordered:
                if post.scheduled_at and post.scheduled_at > datetime.now(timezone.utc):
                    keep = post
                    break
            if keep is None and ordered:
                keep = ordered[-1]
            for post in posts:
                if keep and post.id == keep.id:
                    payload = dict(post.payload or {})
                    payload["repeat_on"] = False
                    post.payload = payload
                    continue
                post.status = "skipped"
        await session.commit()

    async def _mark_processing(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        """Yield exact canonical work before inherited legacy lease claim."""

        if not items or not canonical_publication_delivery_primary_started():
            await super()._mark_processing(session, items)
            return

        selected_ids = [int(post.id) for post in items]
        legacy_candidates: list[PostTask] = []
        for task_id in selected_ids:
            try:
                yielded_repeat = await self._yield_proven_repeat_to_canonical_primary(
                    session,
                    task_id=task_id,
                )
                if yielded_repeat:
                    continue
                yielded_nonrepeat = await self._yield_proven_nonrepeat_to_canonical_primary(
                    session,
                    task_id=task_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await session.rollback()
                logger.warning(
                    "Scheduler: canonical authority proof failed post_id={} type={}",
                    task_id,
                    type(exc).__name__,
                )
                yielded_nonrepeat = False

            if yielded_nonrepeat:
                continue

            post = await session.get(PostTask, task_id, populate_existing=True)
            if post is not None:
                legacy_candidates.append(post)

        items[:] = legacy_candidates
        await super()._mark_processing(session, items)

    async def start(self) -> None:
        await super().start()
        if not self._repeat_continuation_enabled:
            return

        session_factory = self._continuation_session_factory()
        if session_factory is None:
            logger.warning(
                "Canonical repeat continuation requested but scheduler has no session factory"
            )
            return

        worker = self._continuation_worker_factory(session_factory=session_factory)
        try:
            await worker.start()
        except BaseException:
            try:
                await worker.stop()
            except Exception:
                logger.exception(
                    "Boot: failed to clean up canonical repeat continuation worker after startup failure"
                )
            await super().stop()
            raise
        self._repeat_continuation_worker = worker

    async def stop(self) -> None:
        worker = self._repeat_continuation_worker
        self._repeat_continuation_worker = None
        if worker is not None:
            try:
                await worker.stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Shutdown: failed to stop canonical repeat continuation")
        await super().stop()
