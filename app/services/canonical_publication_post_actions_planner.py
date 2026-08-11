from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.telegram_results import normalize_telegram_message_ids


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _runtime_options(
    publication_meta: Mapping[str, Any],
    schedule_meta: Mapping[str, Any],
) -> dict[str, Any] | None:
    publication_raw = publication_meta.get("runtime_options")
    schedule_raw = schedule_meta.get("runtime_options")
    if publication_raw is None and schedule_raw is None:
        return {}
    publication_options = _mapping(publication_raw)
    schedule_options = _mapping(schedule_raw)
    if publication_options is None or schedule_options is None:
        return None
    if publication_options != schedule_options:
        return None
    return publication_options


def _strict_bool(value: Any, *, default: bool = False) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    return None


def _forward_channel_ids(value: Any) -> tuple[int, ...] | None:
    if value in (None, False, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        return None
    result: list[int] = []
    seen: set[int] = set()
    for raw in value:
        if isinstance(raw, bool):
            return None
        try:
            channel_id = int(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if channel_id <= 0 or channel_id in seen:
            return None
        seen.add(channel_id)
        result.append(channel_id)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationForwardTarget:
    channel_id: int
    telegram_chat_id: int


@dataclass(frozen=True, slots=True)
class CanonicalPublicationPostDeliveryActionPlan:
    publication_id: int
    source_channel_id: int
    source_telegram_chat_id: int
    message_ids: tuple[int, ...]
    pin_last_message: bool
    forward_targets: tuple[CanonicalPublicationForwardTarget, ...]
    forward_silent: bool


class CanonicalPublicationPostDeliveryActionPlanner:
    """Pure proof for legacy-compatible best-effort pin/forward actions.

    Historical scheduler pin and forward actions do not decide whether primary
    publication succeeded; provider failures are suppressed. Canonical execution can
    therefore run the same actions after durable terminal success. This planner reads
    only canonical Publication/Schedule/Attempt/Channel state and never ``PostTask``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        publication_id: int,
    ) -> CanonicalPublicationPostDeliveryActionPlan | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        row = (
            await self.session.execute(
                select(Publication, ScheduleEntry, PublicationAttempt, Channel)
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
                .join(Channel, Channel.id == Publication.channel_id)
                .where(
                    Publication.id == safe_publication_id,
                    Publication.status == "published",
                    ScheduleEntry.status == "completed",
                    PublicationAttempt.status == "published",
                    PublicationAttempt.finished_at.is_not(None),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        publication, schedule, attempt, source_channel = row

        publication_ids = normalize_telegram_message_ids(publication.telegram_message_ids)
        attempt_ids = normalize_telegram_message_ids(attempt.telegram_message_ids)
        if not publication_ids or publication_ids != attempt_ids:
            return None

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if publication_meta is None or schedule_meta is None:
            return None
        options = _runtime_options(publication_meta, schedule_meta)
        if options is None:
            return None

        pin_on = _strict_bool(options.get("pin_on"))
        silent = _strict_bool(options.get("silent"))
        forward_ids = _forward_channel_ids(options.get("forward_to"))
        if pin_on is None or silent is None or forward_ids is None:
            return None

        forward_targets: list[CanonicalPublicationForwardTarget] = []
        if forward_ids:
            rows = (
                await self.session.execute(
                    select(Channel).where(Channel.id.in_(list(forward_ids)))
                )
            ).scalars().all()
            by_id = {int(channel.id): channel for channel in rows}
            for channel_id in forward_ids:
                target = by_id.get(int(channel_id))
                if target is None:
                    # Legacy silently skipped missing targets. The canonical planner is
                    # stricter: unresolved configured identity is not a proven action.
                    return None
                forward_targets.append(
                    CanonicalPublicationForwardTarget(
                        channel_id=int(target.id),
                        telegram_chat_id=int(target.tg_chat_id),
                    )
                )

        return CanonicalPublicationPostDeliveryActionPlan(
            publication_id=int(publication.id),
            source_channel_id=int(source_channel.id),
            source_telegram_chat_id=int(source_channel.tg_chat_id),
            message_ids=tuple(publication_ids),
            pin_last_message=bool(pin_on),
            forward_targets=tuple(forward_targets),
            forward_silent=bool(silent),
        )
