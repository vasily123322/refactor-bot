from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.canonical_publication_delivery_authority import (
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
from app.services.canonical_publication_nonrepeat_scheduler_proof import (
    _classify as _classify_supported_profile,
    _profile_started as _nonrepeat_profile_started,
)


class CanonicalSchedulerAdmissionKind(str, Enum):
    LEGACY_UNLINKED = "legacy_unlinked"
    LEGACY_TIME_VIEWS = "legacy_time_views"
    LEGACY_REPORT = "legacy_report"
    LEGACY_ROLLOUT_NOT_STARTED = "legacy_rollout_not_started"
    CANONICAL_PROOF_REQUIRED = "canonical_proof_required"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class CanonicalSchedulerAdmission:
    kind: CanonicalSchedulerAdmissionKind
    publication_id: int | None = None
    profile: str | None = None
    repeat: bool | None = None

    @property
    def legacy_allowed(self) -> bool:
        return self.kind in {
            CanonicalSchedulerAdmissionKind.LEGACY_UNLINKED,
            CanonicalSchedulerAdmissionKind.LEGACY_TIME_VIEWS,
            CanonicalSchedulerAdmissionKind.LEGACY_REPORT,
            CanonicalSchedulerAdmissionKind.LEGACY_ROLLOUT_NOT_STARTED,
        }


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _runtime_options(publication: Publication, schedule: ScheduleEntry) -> dict[str, Any] | None:
    publication_meta = _mapping(publication.meta)
    schedule_meta = _mapping(schedule.meta)
    if publication_meta is None or schedule_meta is None:
        return None
    publication_options = publication_meta.get("runtime_options")
    schedule_options = schedule_meta.get("runtime_options")
    if publication_options is None and schedule_options is None:
        return {}
    publication_options = _mapping(publication_options)
    schedule_options = _mapping(schedule_options)
    if publication_options is None or schedule_options is None:
        return None
    if publication_options != schedule_options:
        return None
    return publication_options


def _repeat_kind(schedule: ScheduleEntry) -> bool | None:
    rule = _mapping(schedule.repeat_rule)
    if rule is None:
        return None
    if not rule:
        return False
    if rule.get("enabled") is not True or not set(rule).issubset({"enabled", "seconds"}):
        return None
    seconds = rule.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        return None
    return True


def _profile_suffix(name: str) -> str:
    if name.startswith("time_"):
        return name.removeprefix("time_")
    if name.startswith("views_"):
        return name.removeprefix("views_")
    if name in {"time", "views"}:
        return ""
    return name


def _is_exact_time_views_fallback(options: dict[str, Any]) -> bool:
    if "autodelete_seconds" not in options or "autodelete_views" not in options:
        return False
    report = options.get("autodelete_report", False)
    if type(report) is not bool:
        return False

    time_options = dict(options)
    time_options.pop("autodelete_views", None)
    time_options["autodelete_report"] = False
    views_options = dict(options)
    views_options.pop("autodelete_seconds", None)
    views_options["autodelete_report"] = False

    time_profile = _classify_supported_profile(time_options)
    views_profile = _classify_supported_profile(views_options)
    if time_profile is None or views_profile is None:
        return False
    return (
        time_profile.name.startswith("time")
        and views_profile.name.startswith("views")
        and _profile_suffix(time_profile.name) == _profile_suffix(views_profile.name)
    )


def _exact_report_fallback_profile(options: dict[str, Any]) -> str | None:
    if options.get("autodelete_report") is not True:
        return None
    without_report = dict(options)
    without_report["autodelete_report"] = False
    profile = _classify_supported_profile(without_report)
    return profile.name if profile is not None else None


def _repeat_profile_started(profile: str) -> bool:
    if profile in {"plain", "pin", "forward", "pin_forward"}:
        return canonical_publication_delivery_repeat_started()
    if profile == "time":
        return canonical_publication_delivery_repeat_time_started()
    if profile == "time_pin":
        return canonical_publication_delivery_repeat_time_pin_started()
    if profile == "time_forward":
        return canonical_publication_delivery_repeat_time_forward_started()
    if profile == "time_pin_forward":
        return canonical_publication_delivery_repeat_time_pin_forward_started()
    if profile == "views":
        return canonical_publication_delivery_repeat_views_started()
    if profile == "views_pin":
        return canonical_publication_delivery_repeat_views_pin_started()
    if profile == "views_forward":
        return canonical_publication_delivery_repeat_views_forward_started()
    if profile == "views_pin_forward":
        return canonical_publication_delivery_repeat_views_pin_forward_started()
    return False


class CanonicalSchedulerAdmissionService:
    """Classify one pending PostTask before any legacy scheduler mutation.

    Linked work is never admitted merely because a canonical ownership proof returned
    ``None``. Only an explicit legacy fallback is allowed through. Once an exact
    canonical profile has a live successfully-started runtime, every parity/state/lease
    ambiguity is left to the canonical proof and fails closed if that proof does not
    succeed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def classify(self, *, task_id: int) -> CanonicalSchedulerAdmission:
        try:
            safe_task_id = int(task_id)
        except (TypeError, ValueError, OverflowError):
            return CanonicalSchedulerAdmission(CanonicalSchedulerAdmissionKind.FAIL_CLOSED)
        if safe_task_id <= 0:
            return CanonicalSchedulerAdmission(CanonicalSchedulerAdmissionKind.FAIL_CLOSED)

        publications = list(
            (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.legacy_post_task_id == safe_task_id)
                    .limit(2)
                )
            ).scalars().all()
        )
        if not publications:
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.LEGACY_UNLINKED
            )
        if len(publications) != 1:
            return CanonicalSchedulerAdmission(CanonicalSchedulerAdmissionKind.FAIL_CLOSED)

        publication = publications[0]
        publication_id = int(publication.id)
        task = await self.session.get(PostTask, safe_task_id, populate_existing=True)
        if (
            task is None
            or str(task.status) != "pending"
            or publication.status != "queued"
            or int(publication.attempt_count or 0) != 0
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
            or publication.schedule_entry_id is None
        ):
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.FAIL_CLOSED,
                publication_id=publication_id,
            )

        schedule = await self.session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id),
            populate_existing=True,
        )
        if (
            schedule is None
            or schedule.status != "pending"
            or int(schedule.channel_id) != int(publication.channel_id)
            or int(schedule.content_item_id) != int(publication.content_item_id)
            or int(schedule.content_revision) != int(publication.content_revision)
        ):
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.FAIL_CLOSED,
                publication_id=publication_id,
            )

        scheduler_lease = await self.session.scalar(
            select(SchedulerTaskLease.id)
            .where(SchedulerTaskLease.task_id == safe_task_id)
            .limit(1)
        )
        canonical_lease = await self.session.scalar(
            select(PublicationDeliveryLease.id)
            .where(PublicationDeliveryLease.publication_id == publication_id)
            .limit(1)
        )
        if scheduler_lease is not None or canonical_lease is not None:
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.FAIL_CLOSED,
                publication_id=publication_id,
            )

        repeat = _repeat_kind(schedule)
        options = _runtime_options(publication, schedule)
        if repeat is None or options is None:
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.FAIL_CLOSED,
                publication_id=publication_id,
            )

        report = options.get("autodelete_report", False)
        if type(report) is not bool:
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.FAIL_CLOSED,
                publication_id=publication_id,
                repeat=repeat,
            )

        if _is_exact_time_views_fallback(options):
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.LEGACY_TIME_VIEWS,
                publication_id=publication_id,
                repeat=repeat,
            )

        report_profile = _exact_report_fallback_profile(options)
        if report_profile is not None:
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.LEGACY_REPORT,
                publication_id=publication_id,
                profile=report_profile,
                repeat=repeat,
            )
        if report:
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.FAIL_CLOSED,
                publication_id=publication_id,
                repeat=repeat,
            )

        profile = _classify_supported_profile(options)
        if profile is None:
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.FAIL_CLOSED,
                publication_id=publication_id,
                repeat=repeat,
            )

        started = (
            _repeat_profile_started(profile.name)
            if repeat
            else _nonrepeat_profile_started(profile.name)
        )
        if not started:
            return CanonicalSchedulerAdmission(
                CanonicalSchedulerAdmissionKind.LEGACY_ROLLOUT_NOT_STARTED,
                publication_id=publication_id,
                profile=profile.name,
                repeat=repeat,
            )

        return CanonicalSchedulerAdmission(
            CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED,
            publication_id=publication_id,
            profile=profile.name,
            repeat=repeat,
        )
