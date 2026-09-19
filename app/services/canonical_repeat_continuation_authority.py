from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_runtime_safety import has_no_replay_barrier


@dataclass(frozen=True, slots=True)
class CanonicalRepeatContinuationAuthority:
    publication: Publication
    schedule: ScheduleEntry
    attempt: PublicationAttempt
    repeat_group_id: int
    repeat_seconds: int
    runtime_options: dict[str, Any]


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _runtime_options(meta: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = meta.get("runtime_options")
    if raw is None:
        return {}
    return _mapping(raw)


class CanonicalRepeatContinuationAuthorityService:
    """Lock and prove exclusive canonical repeat-continuation source authority.

    Selection is only an optimization. Every primitive that can reserve, verify or
    materialize a successor must call this proof in its own transaction. Durable
    no-replay evidence immediately removes a source from continuation authority.

    The proof intentionally binds Publication, Schedule and the exact current Attempt:

    * terminal published/completed/published lifecycle;
    * exact Schedule identity linkage;
    * exact `attempt == Publication.attempt_count` linkage and finished evidence;
    * durable `Attempt.meta.canonical_delivery == true`;
    * exact repeat group/rule/runtime identity shared by Publication and Schedule.

    Any drift returns no authority. Callers decide whether that is ordinary ineligibility
    or a conflict relative to already durable reservation/successor state, but no caller
    may mutate repeat continuation state without this locked proof.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_and_prove(
        self,
        publication_id: int,
    ) -> CanonicalRepeatContinuationAuthority | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        row = (
            await self.session.execute(
                select(Publication, ScheduleEntry, PublicationAttempt)
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                    ),
                )
                .join(
                    PublicationAttempt,
                    and_(
                        PublicationAttempt.publication_id == Publication.id,
                        PublicationAttempt.attempt == Publication.attempt_count,
                    ),
                )
                .where(Publication.id == safe_publication_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            return None
        publication, schedule, attempt = row
        if await has_no_replay_barrier(
            self.session,
            publication_id=safe_publication_id,
        ):
            return None

        if (
            publication.status != "published"
            or schedule.status != "completed"
            or attempt.status != "published"
            or attempt.finished_at is None
        ):
            return None

        attempt_meta = _mapping(attempt.meta)
        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if attempt_meta is None or publication_meta is None or schedule_meta is None:
            return None
        if attempt_meta.get("canonical_delivery") is not True:
            return None

        publication_group_id = _positive_int(publication_meta.get("repeat_group_id"))
        schedule_group_id = _positive_int(schedule_meta.get("repeat_group_id"))
        repeat_rule = _mapping(schedule.repeat_rule)
        repeat_seconds = (
            _positive_int(repeat_rule.get("seconds")) if repeat_rule is not None else None
        )
        publication_options = _runtime_options(publication_meta)
        schedule_options = _runtime_options(schedule_meta)
        if (
            publication_group_id is None
            or schedule_group_id is None
            or publication_group_id != schedule_group_id
            or repeat_rule is None
            or repeat_rule.get("enabled") is not True
            or set(repeat_rule).difference({"enabled", "seconds"})
            or repeat_seconds is None
            or publication_options is None
            or schedule_options is None
            or publication_options != schedule_options
        ):
            return None

        return CanonicalRepeatContinuationAuthority(
            publication=publication,
            schedule=schedule,
            attempt=attempt,
            repeat_group_id=publication_group_id,
            repeat_seconds=repeat_seconds,
            runtime_options=publication_options,
        )
