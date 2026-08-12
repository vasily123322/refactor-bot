from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CUTOVER_META_KEY,
    _FORBIDDEN_EPHEMERAL_KEYS,
    _IDENTITY_MARKERS,
    _expected_transport_payload,
    _identity_markers_match,
    _legacy_intent_matches,
    _mapping,
    _nonrepeat,
    _silent_intent_matches,
    _strip_neutral_effect_fields,
    _supported_runtime_options,
)
from app.services.scheduling import as_utc


_CUTOVER_STATUS = "canonical_cutover"
_TIMER_ALLOWED_RUNTIME_KEYS = frozenset(
    {
        "silent",
        "autodelete_seconds",
        "autodelete_views",
        "autodelete_report",
    }
)
_NEUTRAL_NUMBER_VALUES = (None, False, 0, "0", "")


@dataclass(frozen=True, slots=True)
class CanonicalPublicationAtomicHandoffClaimResult:
    publication_id: int
    outcome: Literal[
        "claimed",
        "ineligible",
        "contention",
        "conflict",
        "claim_unavailable",
    ]
    legacy_post_task_id: int | None = None
    claim: CanonicalPublicationDeliveryClaim | None = None


@dataclass(frozen=True, slots=True)
class _AtomicHandoffRuntimeProfile:
    options: dict[str, Any]
    time_autodelete_seconds: int | None = None

    @property
    def timer_requested(self) -> bool:
        return self.time_autodelete_seconds is not None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _atomic_runtime_profile(
    plan: CanonicalPublicationDeliveryPlan,
    *,
    allow_time_autodelete: bool,
) -> _AtomicHandoffRuntimeProfile | None:
    # Preserve the already-proven empty/silent handoff exactly as before.
    existing = _supported_runtime_options(plan)
    if existing is not None:
        return _AtomicHandoffRuntimeProfile(options=deepcopy(existing))
    if not allow_time_autodelete:
        return None

    try:
        options = plan.runtime_options()
    except (TypeError, ValueError):
        return None
    if not isinstance(options, dict) or not set(options).issubset(
        _TIMER_ALLOWED_RUNTIME_KEYS
    ):
        # Pin/forward and generated effective timer fields remain blocked here.
        return None

    if "silent" in options and type(options.get("silent")) is not bool:
        return None
    seconds = _positive_int(options.get("autodelete_seconds"))
    if seconds is None:
        # This widening is intentionally time-effectful only. Neutral timer fields do not
        # create a second plain profile with subtly different payload rules.
        return None

    views = options.get("autodelete_views")
    if views not in _NEUTRAL_NUMBER_VALUES:
        return None
    report = options.get("autodelete_report", False)
    if type(report) is not bool:
        return None

    return _AtomicHandoffRuntimeProfile(
        options=deepcopy(options),
        time_autodelete_seconds=seconds,
    )


def _generated_timer_state_is_pristine(payload: Mapping[str, Any]) -> bool:
    if payload.get("autodelete_effective_seconds") not in _NEUTRAL_NUMBER_VALUES:
        return False
    if payload.get("autodelete_at") not in (None, ""):
        return False
    if payload.get("autodeleted") not in (None, False):
        return False
    if payload.get("autodeleted_at") not in (None, ""):
        return False
    if payload.get("result_ids") not in (None, []):
        return False
    if payload.get("result_link") not in (None, ""):
        return False
    return True


def _timer_legacy_intent_matches(
    *,
    task: PostTask,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
    profile: _AtomicHandoffRuntimeProfile,
) -> bool:
    if not profile.timer_requested:
        return False
    if int(task.channel_id) != int(plan.channel_id):
        return False
    if task.scheduled_at is None or as_utc(task.scheduled_at) != as_utc(plan.scheduled_at):
        return False
    if task.error not in (None, ""):
        return False

    current = _mapping(task.payload)
    expected = _expected_transport_payload(plan)
    if current is None or expected is None:
        return False
    if any(key in current for key in _FORBIDDEN_EPHEMERAL_KEYS):
        return False
    if not _identity_markers_match(current, publication=publication, plan=plan):
        return False
    if not _silent_intent_matches(current, expected, profile.options):
        return False
    if not _generated_timer_state_is_pristine(current):
        return False

    # Bridge queueing copies base timer intent into PostTask verbatim while reserving
    # generated effective/due fields. Require the exact pre-send representation so a
    # drifted legacy timer cannot silently change behavior during cutover.
    raw_seconds = profile.options.get("autodelete_seconds")
    if (
        "autodelete_seconds" not in current
        or current.get("autodelete_seconds") != raw_seconds
    ):
        return False

    if "autodelete_report" in profile.options:
        if (
            type(current.get("autodelete_report")) is not bool
            or current.get("autodelete_report")
            is not profile.options.get("autodelete_report")
        ):
            return False
    elif current.get("autodelete_report") not in (None, False):
        return False

    if "autodelete_views" in profile.options:
        if current.get("autodelete_views") != profile.options.get("autodelete_views"):
            return False
    elif current.get("autodelete_views") not in _NEUTRAL_NUMBER_VALUES:
        return False

    # Remove the proven timer fields before reusing the established neutral-effects and
    # immutable transport-payload comparison for every other capability.
    current_for_base = deepcopy(current)
    for key in _IDENTITY_MARKERS:
        current_for_base.pop(key, None)
    for key in (
        "autodelete_seconds",
        "autodelete_effective_seconds",
        "autodelete_views",
        "autodelete_report",
        "autodelete_at",
        "autodeleted",
        "autodeleted_at",
        "result_ids",
        "result_link",
    ):
        current_for_base.pop(key, None)

    current_clean = _strip_neutral_effect_fields(current_for_base)
    expected_clean = _strip_neutral_effect_fields(deepcopy(expected))
    return bool(
        current_clean is not None
        and expected_clean is not None
        and current_clean == expected_clean
    )


def _atomic_legacy_intent_matches(
    *,
    task: PostTask,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
    profile: _AtomicHandoffRuntimeProfile,
) -> bool:
    if profile.timer_requested:
        return _timer_legacy_intent_matches(
            task=task,
            publication=publication,
            plan=plan,
            profile=profile,
        )
    return _legacy_intent_matches(
        task=task,
        publication=publication,
        plan=plan,
    )


class CanonicalPublicationAtomicHandoffClaimService:
    """Transfer linked authority and claim canonical delivery in one commit.

    Empty/silent parity remains identical to the established handoff service. When the
    caller proves that the canonical time-autodelete executor is available, this atomic
    path may additionally transfer a pristine, non-repeat, time-only linked timer whose
    base timer/report intent exactly matches the canonical queue-time snapshot and whose
    generated legacy timer/result fields have never been populated.

    Timer capability is checked before the single authority commit. If the dependent
    executor is unavailable or parity fails, rollback restores the PostTask; there is no
    transport-free queued orphan.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rollback_result(
        self,
        publication_id: int,
        outcome: Literal["ineligible", "contention", "conflict", "claim_unavailable"],
        task_id: int | None = None,
    ) -> CanonicalPublicationAtomicHandoffClaimResult:
        await self.session.rollback()
        return CanonicalPublicationAtomicHandoffClaimResult(
            publication_id=int(publication_id),
            outcome=outcome,
            legacy_post_task_id=(int(task_id) if task_id is not None else None),
        )

    async def claim_linked(
        self,
        publication_id: int,
        *,
        holder: str,
        ttl_seconds: int,
        at: datetime | None = None,
        allow_time_autodelete: bool = False,
    ) -> CanonicalPublicationAtomicHandoffClaimResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_publication_id = 0
        if safe_publication_id <= 0:
            return CanonicalPublicationAtomicHandoffClaimResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )
        current = as_utc(at or datetime.now(timezone.utc))

        try:
            publication = (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.id == safe_publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                publication is None
                or publication.status != "queued"
                or int(publication.attempt_count or 0) != 0
                or publication.legacy_post_task_id is None
                or publication.telegram_message_ids not in (None, [])
                or publication.result_link is not None
                or publication.last_error is not None
            ):
                return await self._rollback_result(safe_publication_id, "ineligible")
            task_id = int(publication.legacy_post_task_id)

            claimed_cutover = await self.session.execute(
                update(PostTask)
                .where(
                    PostTask.id == task_id,
                    PostTask.status == "pending",
                )
                .values(status=_CUTOVER_STATUS)
                .execution_options(synchronize_session=False)
            )
            if int(claimed_cutover.rowcount or 0) != 1:
                return await self._rollback_result(
                    safe_publication_id,
                    "contention",
                    task_id,
                )

            task = (
                await self.session.execute(
                    select(PostTask).where(PostTask.id == task_id).with_for_update()
                )
            ).scalar_one_or_none()
            if task is None or str(task.status) != _CUTOVER_STATUS:
                return await self._rollback_result(
                    safe_publication_id,
                    "conflict",
                    task_id,
                )

            scheduler_lease = (
                await self.session.execute(
                    select(SchedulerTaskLease)
                    .where(SchedulerTaskLease.task_id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if scheduler_lease is not None:
                return await self._rollback_result(
                    safe_publication_id,
                    "conflict",
                    task_id,
                )

            canonical_lease = (
                await self.session.execute(
                    select(PublicationDeliveryLease)
                    .where(PublicationDeliveryLease.publication_id == safe_publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if canonical_lease is not None:
                return await self._rollback_result(
                    safe_publication_id,
                    "conflict",
                    task_id,
                )

            if publication.schedule_entry_id is None:
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )
            schedule = (
                await self.session.execute(
                    select(ScheduleEntry)
                    .where(ScheduleEntry.id == int(publication.schedule_entry_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            item = (
                await self.session.execute(
                    select(ContentItem)
                    .where(ContentItem.id == int(publication.content_item_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            revision = (
                await self.session.execute(
                    select(ContentRevision)
                    .where(
                        ContentRevision.content_item_id == int(publication.content_item_id),
                        ContentRevision.revision == int(publication.content_revision),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            channel = (
                await self.session.execute(
                    select(Channel)
                    .where(Channel.id == int(publication.channel_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None or item is None or revision is None or channel is None:
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )

            plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
                safe_publication_id,
                at=current,
            )
            profile = (
                _atomic_runtime_profile(
                    plan,
                    allow_time_autodelete=allow_time_autodelete,
                )
                if plan is not None
                else None
            )
            if (
                plan is None
                or int(plan.schedule_entry_id) != int(schedule.id)
                or int(plan.content_item_id) != int(item.id)
                or int(plan.content_revision) != int(revision.revision)
                or int(plan.channel_id) != int(channel.id)
                or profile is None
                or not _nonrepeat(plan)
                or not _atomic_legacy_intent_matches(
                    task=task,
                    publication=publication,
                    plan=plan,
                    profile=profile,
                )
            ):
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )

            schedule_meta = _mapping(schedule.meta)
            publication_meta = _mapping(publication.meta)
            if schedule_meta is None or publication_meta is None:
                return await self._rollback_result(
                    safe_publication_id,
                    "ineligible",
                    task_id,
                )
            legacy_marker = schedule_meta.get("legacy_post_task_id")
            if legacy_marker is not None:
                if isinstance(legacy_marker, bool):
                    return await self._rollback_result(
                        safe_publication_id,
                        "conflict",
                        task_id,
                    )
                try:
                    if int(legacy_marker) != task_id:
                        return await self._rollback_result(
                            safe_publication_id,
                            "conflict",
                            task_id,
                        )
                except (TypeError, ValueError, OverflowError):
                    return await self._rollback_result(
                        safe_publication_id,
                        "conflict",
                        task_id,
                    )
                schedule_meta.pop("legacy_post_task_id", None)

            cutover_meta = {
                "retired": True,
                "retired_at": current.isoformat(),
                "legacy_post_task_id": task_id,
                "source_status": "pending",
                "atomic_claim": True,
                "time_autodelete": bool(profile.timer_requested),
            }
            schedule.meta = {
                **schedule_meta,
                CUTOVER_META_KEY: deepcopy(cutover_meta),
            }
            publication.meta = {
                **publication_meta,
                CUTOVER_META_KEY: deepcopy(cutover_meta),
            }
            publication.legacy_post_task_id = None
            await self.session.delete(task)

            claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                self.session
            ).claim_supported(
                publication_id=safe_publication_id,
                holder=holder,
                ttl_seconds=ttl_seconds,
                now=current,
                allow_time_autodelete=allow_time_autodelete,
            )
            if claim is None:
                await self.session.rollback()
                return CanonicalPublicationAtomicHandoffClaimResult(
                    publication_id=safe_publication_id,
                    outcome="claim_unavailable",
                    legacy_post_task_id=task_id,
                )

            return CanonicalPublicationAtomicHandoffClaimResult(
                publication_id=safe_publication_id,
                outcome="claimed",
                legacy_post_task_id=task_id,
                claim=claim,
            )
        except Exception:
            await self.session.rollback()
            raise
