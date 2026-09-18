from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import Channel, Client
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.publication_execution_mode import CANONICAL_EXECUTION_MODE
from app.services.scheduling import as_utc


class ContentPlanPublicationControlError(RuntimeError):
    pass


class ContentPlanStaleControl(ContentPlanPublicationControlError):
    pass


class ContentPlanRepeatUnsupported(ContentPlanPublicationControlError):
    pass


@dataclass(frozen=True, slots=True)
class ContentPlanPublicationControlResult:
    publication_id: int
    schedule_entry_id: int
    scheduled_at: datetime
    repeat_rule: dict[str, Any]
    timezone: str | None


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(value: int) -> str:
    number = int(value)
    if number < 0:
        raise ValueError("base36 value must be non-negative")
    if number == 0:
        return "0"
    chars: list[str] = []
    while number:
        number, remainder = divmod(number, 36)
        chars.append(_BASE36_DIGITS[remainder])
    return "".join(reversed(chars))


def content_plan_schedule_state_token(
    *,
    schedule_entry_id: int,
    scheduled_at: datetime,
    repeat_rule: Mapping[str, Any] | None,
) -> str:
    """Compact CAS token derived only from authoritative ScheduleEntry state."""

    state = {
        "schedule_entry_id": int(schedule_entry_id),
        "scheduled_at": as_utc(scheduled_at).isoformat(timespec="microseconds"),
        "repeat_rule": _mapping(repeat_rule),
    }
    payload = json.dumps(
        state,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.blake2b(
        payload,
        digest_size=8,
        person=b"cp-state-v1",
    ).digest()
    return _base36(int.from_bytes(digest, "big"))


class ContentPlanPublicationControlService:
    """Canonical Content Plan mutations for a queued Publication occurrence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _owned_pending(
        self,
        session: AsyncSession,
        *,
        publication_id: int,
        tg_user_id: int,
    ) -> tuple[Publication, ScheduleEntry] | None:
        try:
            safe_publication_id = int(publication_id)
            safe_user_id = int(tg_user_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0 or safe_user_id <= 0:
            return None

        owned = (
            await session.execute(
                select(Publication, ScheduleEntry)
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                    ),
                )
                .join(Channel, Channel.id == Publication.channel_id)
                .join(Client, Client.id == Channel.owner_id)
                .where(
                    Publication.id == safe_publication_id,
                    Client.tg_user_id == safe_user_id,
                    Publication.execution_mode == CANONICAL_EXECUTION_MODE,
                    Publication.status == "queued",
                    ScheduleEntry.status == "pending",
                )
                .with_for_update()
            )
        ).one_or_none()
        if owned is None:
            return None

        publication, _schedule = owned
        if (
            int(publication.attempt_count or 0) != 0
            or publication.result_link is not None
            or publication.last_error is not None
            or publication.telegram_message_ids not in (None, [])
        ):
            return None
        attempt_id = await session.scalar(
            select(PublicationAttempt.id)
            .where(PublicationAttempt.publication_id == int(publication.id))
            .limit(1)
        )
        if attempt_id is not None:
            return None
        lease_id = await session.scalar(
            select(PublicationDeliveryLease.publication_id)
            .where(PublicationDeliveryLease.publication_id == int(publication.id))
            .limit(1)
        )
        if lease_id is not None:
            return None
        return owned

    @staticmethod
    def _result(
        publication: Publication,
        schedule: ScheduleEntry,
    ) -> ContentPlanPublicationControlResult:
        return ContentPlanPublicationControlResult(
            publication_id=int(publication.id),
            schedule_entry_id=int(schedule.id),
            scheduled_at=as_utc(schedule.scheduled_at),
            repeat_rule=deepcopy(_mapping(schedule.repeat_rule)),
            timezone=str(schedule.timezone) if schedule.timezone else None,
        )

    @staticmethod
    def _require_expected_schedule_state(
        schedule: ScheduleEntry,
        expected_schedule_token: str,
    ) -> None:
        expected = str(expected_schedule_token or "").strip().lower()
        actual = content_plan_schedule_state_token(
            schedule_entry_id=int(schedule.id),
            scheduled_at=schedule.scheduled_at,
            repeat_rule=_mapping(schedule.repeat_rule),
        )
        if not expected or expected != actual:
            raise ContentPlanStaleControl("stale canonical schedule control")

    @staticmethod
    async def _guard_control_transaction(
        session: AsyncSession,
        *,
        publication: Publication,
        schedule: ScheduleEntry,
    ) -> None:
        publication_guard = await session.execute(
            update(Publication)
            .where(
                Publication.id == int(publication.id),
                Publication.execution_mode == CANONICAL_EXECUTION_MODE,
                Publication.status == "queued",
                Publication.attempt_count == 0,
                Publication.result_link.is_(None),
                Publication.last_error.is_(None),
            )
            .values(status="queued")
            .execution_options(synchronize_session=False)
        )
        if int(publication_guard.rowcount or 0) != 1:
            raise ContentPlanPublicationControlError(
                "canonical delivery claim won the control race"
            )

        schedule_guard = await session.execute(
            update(ScheduleEntry)
            .where(
                ScheduleEntry.id == int(schedule.id),
                ScheduleEntry.status == "pending",
                ScheduleEntry.scheduled_at == schedule.scheduled_at,
            )
            .values(status="pending")
            .execution_options(synchronize_session=False)
        )
        if int(schedule_guard.rowcount or 0) != 1:
            raise ContentPlanStaleControl("canonical schedule changed during control")

    async def reschedule(
        self,
        *,
        publication_id: int,
        tg_user_id: int,
        expected_schedule_token: str,
        delta_seconds: int,
    ) -> ContentPlanPublicationControlResult:
        seconds = _positive_int(delta_seconds)
        if seconds is None:
            raise ContentPlanPublicationControlError("reschedule interval must be positive")

        async with self.session_factory() as session:
            owned = await self._owned_pending(
                session,
                publication_id=publication_id,
                tg_user_id=tg_user_id,
            )
            if owned is None:
                raise ContentPlanPublicationControlError(
                    "publication is not an owned canonical pending occurrence"
                )
            publication, schedule = owned
            self._require_expected_schedule_state(schedule, expected_schedule_token)
            await self._guard_control_transaction(
                session,
                publication=publication,
                schedule=schedule,
            )
            schedule.scheduled_at = as_utc(schedule.scheduled_at) + timedelta(
                seconds=seconds
            )
            await session.commit()
            await session.refresh(schedule)
            return self._result(publication, schedule)

    async def set_repeat(
        self,
        *,
        publication_id: int,
        tg_user_id: int,
        repeat_seconds: int | None,
        expected_schedule_token: str,
    ) -> ContentPlanPublicationControlResult:
        if repeat_seconds is None:
            seconds = None
        else:
            seconds = _positive_int(repeat_seconds)
            if seconds is None:
                raise ContentPlanPublicationControlError("repeat interval must be positive")

        async with self.session_factory() as session:
            owned = await self._owned_pending(
                session,
                publication_id=publication_id,
                tg_user_id=tg_user_id,
            )
            if owned is None:
                raise ContentPlanPublicationControlError(
                    "publication is not an owned canonical pending occurrence"
                )
            publication, schedule = owned
            self._require_expected_schedule_state(schedule, expected_schedule_token)
            await self._guard_control_transaction(
                session,
                publication=publication,
                schedule=schedule,
            )

            publication_meta = deepcopy(_mapping(publication.meta))
            schedule_meta = deepcopy(_mapping(schedule.meta))
            publication_runtime_raw = publication_meta.get("runtime_options")
            schedule_runtime_raw = schedule_meta.get("runtime_options")
            publication_runtime = _mapping(publication_runtime_raw)
            schedule_runtime = _mapping(schedule_runtime_raw)
            if (
                publication_runtime_raw is not None
                and schedule_runtime_raw is not None
                and publication_runtime != schedule_runtime
            ):
                raise ContentPlanPublicationControlError(
                    "publication/schedule runtime intent mismatch"
                )
            runtime_options = (
                publication_runtime
                if publication_runtime_raw is not None
                else schedule_runtime
            )
            if seconds is not None:
                time_delete = _positive_int(runtime_options.get("autodelete_seconds"))
                views_delete = _positive_int(runtime_options.get("autodelete_views"))
                if time_delete is not None and views_delete is not None:
                    # #509 intentionally keeps repeat+mixed unsupported. Reject before
                    # touching either canonical repeat location and never synthesize a
                    # legacy transport owner.
                    raise ContentPlanRepeatUnsupported(
                        "repeat is unsupported for mixed time+views autodelete"
                    )

                group_id = (
                    _positive_int(schedule_meta.get("repeat_group_id"))
                    or _positive_int(publication_meta.get("repeat_group_id"))
                    or int(publication.id)
                )
                schedule.repeat_rule = {
                    "enabled": True,
                    "seconds": int(seconds),
                }
                schedule_meta["repeat_group_id"] = int(group_id)
                publication_meta["repeat_group_id"] = int(group_id)
            else:
                group_id = (
                    _positive_int(schedule_meta.get("repeat_group_id"))
                    or _positive_int(publication_meta.get("repeat_group_id"))
                )
                if group_id is not None:
                    group_rows = (
                        await session.execute(
                            select(Publication, ScheduleEntry)
                            .join(
                                ScheduleEntry,
                                and_(
                                    ScheduleEntry.id == Publication.schedule_entry_id,
                                    ScheduleEntry.channel_id == Publication.channel_id,
                                    ScheduleEntry.content_item_id == Publication.content_item_id,
                                    ScheduleEntry.content_revision
                                    == Publication.content_revision,
                                ),
                            )
                            .where(
                                Publication.channel_id == int(publication.channel_id),
                                Publication.execution_mode
                                == CANONICAL_EXECUTION_MODE,
                                Publication.status == "queued",
                                ScheduleEntry.status == "pending",
                                ScheduleEntry.meta["repeat_group_id"].as_integer()
                                == int(group_id),
                            )
                            .with_for_update()
                        )
                    ).all()
                    for group_publication, group_schedule in group_rows:
                        group_schedule.repeat_rule = {}
                        group_schedule_meta = deepcopy(_mapping(group_schedule.meta))
                        group_schedule_meta.pop("repeat_group_id", None)
                        group_schedule.meta = group_schedule_meta
                        group_publication_meta = deepcopy(
                            _mapping(group_publication.meta)
                        )
                        group_publication_meta.pop("repeat_group_id", None)
                        group_publication.meta = group_publication_meta
                else:
                    schedule.repeat_rule = {}
                    schedule_meta.pop("repeat_group_id", None)
                    publication_meta.pop("repeat_group_id", None)

            if seconds is not None or group_id is None:
                schedule.meta = schedule_meta
                publication.meta = publication_meta
            await session.commit()
            await session.refresh(schedule)
            await session.refresh(publication)
            return self._result(publication, schedule)
