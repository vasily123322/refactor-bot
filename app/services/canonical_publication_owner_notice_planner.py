from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel, ChannelSettings, Client
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.publication_editor import publication_open_callback
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


def _as_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _safe_nonrepeat(rule) -> bool:
    if rule is None:
        return True
    if not isinstance(rule, dict):
        return False
    return rule.get("enabled") in (None, False, 0)


def _local_time(current: datetime, tz_code: str | None) -> datetime:
    if not tz_code:
        return current
    try:
        return current.astimezone(ZoneInfo(tz_code))
    except Exception:
        try:
            from app.core.timezone import offset_minutes_from_tz

            return current + timedelta(minutes=offset_minutes_from_tz(tz_code))
        except Exception:
            return current


@dataclass(frozen=True, slots=True)
class CanonicalPublicationOwnerNoticePlan:
    publication_id: int
    owner_tg_user_id: int
    owner_username: str | None
    channel_title: str
    source_telegram_chat_id: int
    result_link: str | None
    delivered_count: int
    timezone_code: str
    local_date_iso: str
    local_date_text: str
    local_time_text: str
    callback_data: str


class CanonicalPublicationOwnerNoticePlanner:
    """Pure canonical proof for the historical non-repeat published owner notice."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        publication_id: int,
        *,
        at: datetime | None = None,
    ) -> CanonicalPublicationOwnerNoticePlan | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        row = (
            await self.session.execute(
                select(
                    Publication,
                    ScheduleEntry,
                    PublicationAttempt,
                    Channel,
                    Client,
                )
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
                .join(Client, Client.id == Channel.owner_id)
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
        publication, schedule, attempt, channel, owner = row
        if not _safe_nonrepeat(schedule.repeat_rule):
            return None

        publication_ids = normalize_telegram_message_ids(publication.telegram_message_ids)
        attempt_ids = normalize_telegram_message_ids(attempt.telegram_message_ids)
        if not publication_ids or publication_ids != attempt_ids:
            return None
        result_link = normalize_telegram_result_link(publication.result_link)
        if publication.result_link is not None and result_link is None:
            return None

        settings = (
            await self.session.execute(
                select(ChannelSettings).where(
                    ChannelSettings.channel_id == int(channel.id)
                )
            )
        ).scalar_one_or_none()
        filters = dict(getattr(settings, "filters", {}) or {}) if settings else {}
        raw_tz = filters.get("tz")
        tz_code = str(raw_tz).strip() if raw_tz else "UTC"
        current = _as_utc(at)
        local = _local_time(current, tz_code)
        date_iso = local.date().isoformat()

        return CanonicalPublicationOwnerNoticePlan(
            publication_id=int(publication.id),
            owner_tg_user_id=int(owner.tg_user_id),
            owner_username=(str(owner.username) if owner.username else None),
            channel_title=str(channel.title or channel.tg_chat_id),
            source_telegram_chat_id=int(channel.tg_chat_id),
            result_link=result_link,
            delivered_count=len(publication_ids),
            timezone_code=tz_code,
            local_date_iso=date_iso,
            local_date_text=local.strftime("%d.%m.%Y"),
            local_time_text=local.strftime("%H:%M"),
            callback_data=publication_open_callback(int(publication.id), date_iso),
        )
