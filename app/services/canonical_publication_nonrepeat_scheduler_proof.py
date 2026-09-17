from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    _authority_intent_matches,
    _mapping,
    _nonrepeat,
)
from app.services.canonical_publication_linked_forward_parity import (
    CanonicalPublicationLinkedForwardParityService,
)
from app.services.canonical_publication_nonrepeat_authority import (
    canonical_publication_delivery_nonrepeat_forward_started,
    canonical_publication_delivery_nonrepeat_pin_forward_started,
    canonical_publication_delivery_nonrepeat_pin_started,
    canonical_publication_delivery_nonrepeat_plain_started,
    canonical_publication_delivery_nonrepeat_time_forward_started,
    canonical_publication_delivery_nonrepeat_time_pin_forward_started,
    canonical_publication_delivery_nonrepeat_time_pin_started,
    canonical_publication_delivery_nonrepeat_time_started,
    canonical_publication_delivery_nonrepeat_views_forward_started,
    canonical_publication_delivery_nonrepeat_views_pin_forward_started,
    canonical_publication_delivery_nonrepeat_views_pin_started,
    canonical_publication_delivery_nonrepeat_views_started,
)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationNonrepeatSchedulerProof:
    publication_id: int
    legacy_post_task_id: int
    profile: str


@dataclass(frozen=True, slots=True)
class _Profile:
    name: str
    pin_on: bool
    forward_ids: tuple[int, ...]
    time_seconds: int | None
    views_threshold: int | None
    autodelete_report: bool


_ALLOWED_RUNTIME_KEYS = frozenset(
    {
        "silent",
        "pin_on",
        "forward_to",
        "autodelete_seconds",
        "autodelete_views",
        "autodelete_report",
    }
)


def _profile_started(name: str) -> bool:
    if name == "plain":
        return canonical_publication_delivery_nonrepeat_plain_started()
    if name == "pin":
        return canonical_publication_delivery_nonrepeat_pin_started()
    if name == "forward":
        return canonical_publication_delivery_nonrepeat_forward_started()
    if name == "pin_forward":
        return canonical_publication_delivery_nonrepeat_pin_forward_started()
    if name == "time":
        return canonical_publication_delivery_nonrepeat_time_started()
    if name == "time_pin":
        return canonical_publication_delivery_nonrepeat_time_pin_started()
    if name == "time_forward":
        return canonical_publication_delivery_nonrepeat_time_forward_started()
    if name == "time_pin_forward":
        return canonical_publication_delivery_nonrepeat_time_pin_forward_started()
    if name == "views":
        return canonical_publication_delivery_nonrepeat_views_started()
    if name == "views_pin":
        return canonical_publication_delivery_nonrepeat_views_pin_started()
    if name == "views_forward":
        return canonical_publication_delivery_nonrepeat_views_forward_started()
    if name == "views_pin_forward":
        return canonical_publication_delivery_nonrepeat_views_pin_forward_started()
    return False


def _strict_positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return int(value)


def _classify(options: dict[str, Any]) -> _Profile | None:
    if not set(options).issubset(_ALLOWED_RUNTIME_KEYS):
        return None
    if "silent" in options and type(options.get("silent")) is not bool:
        return None
    if "pin_on" in options and type(options.get("pin_on")) is not bool:
        return None
    if options.get("pin_on") is False:
        return None

    report = options.get("autodelete_report", False)
    if type(report) is not bool:
        return None

    pin_on = options.get("pin_on") is True
    forward_ids: tuple[int, ...] = ()
    if "forward_to" in options:
        raw_forward = options.get("forward_to")
        if not isinstance(raw_forward, list) or not raw_forward:
            return None
        normalized: list[int] = []
        for raw in raw_forward:
            if isinstance(raw, bool):
                return None
            try:
                channel_id = int(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if channel_id <= 0:
                return None
            normalized.append(channel_id)
        forward_ids = tuple(normalized)

    time_seconds = None
    if "autodelete_seconds" in options:
        time_seconds = _strict_positive(options.get("autodelete_seconds"))
        if time_seconds is None:
            return None
    views_threshold = None
    if "autodelete_views" in options:
        views_threshold = _strict_positive(options.get("autodelete_views"))
        if views_threshold is None:
            return None
    if time_seconds is not None and views_threshold is not None:
        return None
    if report and time_seconds is None and views_threshold is None:
        return None

    if time_seconds is not None:
        stem = "time"
    elif views_threshold is not None:
        stem = "views"
    else:
        stem = ""

    suffix = ""
    if pin_on and forward_ids:
        suffix = "pin_forward"
    elif pin_on:
        suffix = "pin"
    elif forward_ids:
        suffix = "forward"

    if not stem:
        name = suffix or "plain"
    elif suffix:
        name = f"{stem}_{suffix}"
    else:
        name = stem

    expected_keys = {"silent"}
    if pin_on:
        expected_keys.add("pin_on")
    if forward_ids:
        expected_keys.add("forward_to")
    if time_seconds is not None:
        expected_keys.add("autodelete_seconds")
    if views_threshold is not None:
        expected_keys.add("autodelete_views")
    if "autodelete_report" in options:
        expected_keys.add("autodelete_report")
    if set(options) - expected_keys:
        return None

    return _Profile(
        name=name,
        pin_on=pin_on,
        forward_ids=forward_ids,
        time_seconds=time_seconds,
        views_threshold=views_threshold,
        autodelete_report=report,
    )


class CanonicalPublicationNonrepeatSchedulerProofService:
    """Read-only proof that one pending legacy task may yield to canonical primary.

    This service owns no cutover mutation: no provider call, lease acquisition, row write,
    commit or rollback. The canonical worker independently wins the atomic handoff/claim
    after scheduler yield. Future profile classifiers may exist here while their dedicated
    started facts remain False; those branches therefore stay fail-closed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def prove_plain(
        self,
        *,
        task_id: int,
        at: datetime | None = None,
    ) -> CanonicalPublicationNonrepeatSchedulerProof | None:
        """Compatibility entrypoint for the scheduler; prove any enabled exact profile."""

        try:
            safe_task_id = int(task_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_task_id <= 0:
            return None

        publications = list(
            (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.legacy_post_task_id == safe_task_id)
                    .limit(2)
                )
            ).scalars().all()
        )
        if len(publications) != 1:
            return None
        publication = publications[0]
        if (
            publication.status != "queued"
            or int(publication.attempt_count or 0) != 0
            or publication.legacy_post_task_id is None
            or int(publication.legacy_post_task_id) != safe_task_id
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
            or publication.schedule_entry_id is None
        ):
            return None

        task = await self.session.get(PostTask, safe_task_id, populate_existing=True)
        if task is None or str(task.status) != "pending":
            return None

        scheduler_lease = (
            await self.session.execute(
                select(SchedulerTaskLease).where(
                    SchedulerTaskLease.task_id == safe_task_id
                )
            )
        ).scalar_one_or_none()
        if scheduler_lease is not None:
            return None
        canonical_lease = (
            await self.session.execute(
                select(PublicationDeliveryLease).where(
                    PublicationDeliveryLease.publication_id == int(publication.id)
                )
            )
        ).scalar_one_or_none()
        if canonical_lease is not None:
            return None

        schedule = await self.session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id),
            populate_existing=True,
        )
        if schedule is None:
            return None
        schedule_meta = _mapping(schedule.meta)
        publication_meta = _mapping(publication.meta)
        if schedule_meta is None or publication_meta is None:
            return None
        legacy_marker = schedule_meta.get("legacy_post_task_id")
        if legacy_marker is not None:
            if isinstance(legacy_marker, bool):
                return None
            try:
                if int(legacy_marker) != safe_task_id:
                    return None
            except (TypeError, ValueError, OverflowError):
                return None

        plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
            int(publication.id),
            at=at,
        )
        if (
            plan is None
            or int(plan.schedule_entry_id) != int(schedule.id)
            or int(plan.content_item_id) != int(publication.content_item_id)
            or int(plan.content_revision) != int(publication.content_revision)
            or int(plan.channel_id) != int(publication.channel_id)
            or not _nonrepeat(plan)
        ):
            return None
        try:
            runtime_options = plan.runtime_options()
        except (TypeError, ValueError):
            return None
        if not isinstance(runtime_options, dict):
            return None
        profile = _classify(runtime_options)
        if profile is None or not _profile_started(profile.name):
            return None

        if profile.forward_ids:
            parity = await CanonicalPublicationLinkedForwardParityService(
                self.session
            ).prove(
                task=task,
                publication=publication,
                plan=plan,
            )
            if (
                parity is None
                or tuple(parity.forward_channel_ids) != profile.forward_ids
                or bool(parity.pin_on) is not profile.pin_on
                or parity.time_autodelete_seconds != profile.time_seconds
                or parity.views_autodelete_threshold != profile.views_threshold
                or bool(parity.autodelete_report) is not profile.autodelete_report
            ):
                return None
            capability = parse_canonical_publication_delivery_runtime_capability(
                runtime_options
            )
            if capability is None:
                return None
            combined_views = profile.name == "views_pin_forward"
            if bool(capability.views_pin_forward_composed) is not combined_views:
                return None
        elif not _authority_intent_matches(
            task=task,
            publication=publication,
            plan=plan,
            allow_time_autodelete=profile.time_seconds is not None,
            allow_views_autodelete=profile.views_threshold is not None,
        ):
            return None

        return CanonicalPublicationNonrepeatSchedulerProof(
            publication_id=int(publication.id),
            legacy_post_task_id=safe_task_id,
            profile=profile.name,
        )
