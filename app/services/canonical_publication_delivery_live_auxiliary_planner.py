from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem
from app.domain.models import Channel, ChannelSettings, Client
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.admin import AdminConfigRepo
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.publication_editor import publication_open_callback
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _mapping(value) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    return dict(value)


def _safe_nonrepeat(rule) -> bool:
    if not isinstance(rule, Mapping):
        return False
    enabled = dict(rule).get("enabled")
    return enabled in (None, False, 0)


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
class CanonicalPublicationLiveOwnerNoticePlan:
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


@dataclass(frozen=True, slots=True)
class CanonicalPublicationLiveAdminLogPlan:
    publication_id: int
    log_chat_id: int
    source_telegram_chat_id: int
    primary_message_id: int
    result_link: str | None
    author_tg_user_id: int | None
    author_username: str | None
    author_full_name: str | None


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryLiveAuxiliaryPlan:
    publication_id: int
    owner_notice: CanonicalPublicationLiveOwnerNoticePlan | None
    admin_log: CanonicalPublicationLiveAdminLogPlan | None


class CanonicalPublicationDeliveryLiveAuxiliaryPlanner:
    """Plan owner/admin auxiliaries while canonical delivery ownership is still live.

    Historical delivery performed admin logging and owner notification after the primary
    Telegram send but before marking the PostTask terminal. This planner reproduces that
    authority boundary without reading PostTask: it requires the exact live canonical
    delivery lease, the unfinished canonical attempt and the immutable post-send context.

    The returned plans are one-shot snapshots. They are not durable retry tokens; owner
    notices and admin logs are non-idempotent and must be executed only inside the same
    live delivery lifecycle.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
        *,
        at: datetime | None = None,
    ) -> CanonicalPublicationDeliveryLiveAuxiliaryPlan | None:
        try:
            publication_id = int(context.publication_id)
            plan_publication_id = int(context.plan.publication_id)
            schedule_entry_id = int(context.plan.schedule_entry_id)
            channel_id = int(context.plan.channel_id)
            content_item_id = int(context.plan.content_item_id)
            content_revision = int(context.plan.content_revision)
            source_chat_id = int(context.plan.telegram_chat_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if publication_id <= 0 or publication_id != plan_publication_id:
            return None

        ids = normalize_telegram_message_ids(context.message_ids)
        if not ids:
            return None
        result_link = normalize_telegram_result_link(context.result_link)
        if context.result_link is not None and result_link is None:
            return None

        current = _utc(at)
        lease = (
            await self.session.execute(
                select(PublicationDeliveryLease).where(
                    PublicationDeliveryLease.publication_id == publication_id,
                    PublicationDeliveryLease.lease_token == str(context.lease.lease_token),
                    PublicationDeliveryLease.expires_at > current,
                )
            )
        ).scalar_one_or_none()
        if lease is None:
            return None

        publication = await self.session.get(Publication, publication_id)
        if (
            publication is None
            or publication.status != "sending"
            or int(publication.attempt_count or 0) != 1
            or int(publication.schedule_entry_id or 0) != schedule_entry_id
            or int(publication.channel_id) != channel_id
            or int(publication.content_item_id) != content_item_id
            or int(publication.content_revision) != content_revision
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
        ):
            return None

        schedule = await self.session.get(ScheduleEntry, schedule_entry_id)
        if (
            schedule is None
            or schedule.status != "pending"
            or int(schedule.channel_id) != channel_id
            or int(schedule.content_item_id) != content_item_id
            or int(schedule.content_revision) != content_revision
            or _utc(schedule.scheduled_at) != _utc(context.plan.scheduled_at)
            or schedule.timezone != context.plan.timezone
        ):
            return None

        item = await self.session.get(ContentItem, content_item_id)
        if (
            item is None
            or int(item.channel_id) != channel_id
            or str(item.kind) != "post"
        ):
            return None

        channel = await self.session.get(Channel, channel_id)
        if (
            channel is None
            or channel.is_active is not True
            or int(channel.tg_chat_id) != source_chat_id
        ):
            return None

        attempt = (
            await self.session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id,
                    PublicationAttempt.attempt == 1,
                )
            )
        ).scalar_one_or_none()
        if (
            attempt is None
            or attempt.status != "sending"
            or attempt.finished_at is not None
            or attempt.telegram_message_ids is not None
            or attempt.error is not None
            or not isinstance(attempt.meta, Mapping)
            or dict(attempt.meta).get("canonical_delivery") is not True
        ):
            return None

        owner_notice = await self._owner_notice(
            context,
            channel=channel,
            message_ids=ids,
            result_link=result_link,
        )
        admin_log = await self._admin_log(
            context,
            item=item,
            message_ids=ids,
            result_link=result_link,
        )
        return CanonicalPublicationDeliveryLiveAuxiliaryPlan(
            publication_id=publication_id,
            owner_notice=owner_notice,
            admin_log=admin_log,
        )

    async def _owner_notice(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
        *,
        channel: Channel,
        message_ids: list[int],
        result_link: str | None,
    ) -> CanonicalPublicationLiveOwnerNoticePlan | None:
        try:
            repeat_rule = context.plan.repeat_rule()
        except (TypeError, ValueError):
            return None
        if not _safe_nonrepeat(repeat_rule):
            return None

        owner = await self.session.get(Client, int(channel.owner_id))
        if owner is None:
            return None
        try:
            owner_tg_user_id = int(owner.tg_user_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if owner_tg_user_id <= 0:
            return None

        settings = (
            await self.session.execute(
                select(ChannelSettings).where(ChannelSettings.channel_id == int(channel.id))
            )
        ).scalar_one_or_none()
        if settings is None:
            filters: dict = {}
        else:
            filters = _mapping(getattr(settings, "filters", None))
            if filters is None:
                return None
        raw_tz = filters.get("tz")
        tz_code = str(raw_tz).strip() if raw_tz else "UTC"
        local = _local_time(_utc(context.primary_finished_at), tz_code)
        date_iso = local.date().isoformat()

        return CanonicalPublicationLiveOwnerNoticePlan(
            publication_id=int(context.publication_id),
            owner_tg_user_id=owner_tg_user_id,
            owner_username=(str(owner.username) if owner.username else None),
            channel_title=str(channel.title or channel.tg_chat_id),
            source_telegram_chat_id=int(channel.tg_chat_id),
            result_link=result_link,
            delivered_count=len(message_ids),
            timezone_code=tz_code,
            local_date_iso=date_iso,
            local_date_text=local.strftime("%d.%m.%Y"),
            local_time_text=local.strftime("%H:%M"),
            callback_data=publication_open_callback(int(context.publication_id), date_iso),
        )

    async def _admin_log(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
        *,
        item: ContentItem,
        message_ids: list[int],
        result_link: str | None,
    ) -> CanonicalPublicationLiveAdminLogPlan | None:
        log_chat_id = await AdminConfigRepo(self.session).get_log_chat_id()
        if log_chat_id is None:
            return None

        author_tg_user_id: int | None = None
        author_username: str | None = None
        author_full_name: str | None = None
        raw_author_id = getattr(item, "created_by_tg_user_id", None)
        try:
            parsed_author_id = int(raw_author_id) if raw_author_id is not None else 0
        except (TypeError, ValueError, OverflowError):
            parsed_author_id = 0
        if parsed_author_id > 0:
            author_tg_user_id = parsed_author_id
            author = (
                await self.session.execute(
                    select(Client).where(Client.tg_user_id == parsed_author_id)
                )
            ).scalar_one_or_none()
            if author is not None:
                author_username = str(author.username) if author.username else None
                author_full_name = str(author.full_name) if author.full_name else None

        return CanonicalPublicationLiveAdminLogPlan(
            publication_id=int(context.publication_id),
            log_chat_id=int(log_chat_id),
            source_telegram_chat_id=int(context.plan.telegram_chat_id),
            primary_message_id=int(message_ids[-1]),
            result_link=result_link,
            author_tg_user_id=author_tg_user_id,
            author_username=author_username,
            author_full_name=author_full_name,
        )
