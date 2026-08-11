from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.admin import AdminConfigRepo
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationAdminLogPlan:
    publication_id: int
    log_chat_id: int
    source_telegram_chat_id: int
    primary_message_id: int
    result_link: str | None
    author_tg_user_id: int | None
    author_username: str | None
    author_full_name: str | None


class CanonicalPublicationAdminLogPlanner:
    """Pure canonical proof for optional post-publication admin logging."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        publication_id: int,
    ) -> CanonicalPublicationAdminLogPlan | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        log_chat_id = await AdminConfigRepo(self.session).get_log_chat_id()
        if log_chat_id is None:
            return None

        row = (
            await self.session.execute(
                select(
                    Publication,
                    ScheduleEntry,
                    PublicationAttempt,
                    Channel,
                    ContentItem,
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
                .join(
                    ContentItem,
                    and_(
                        ContentItem.id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                    ),
                )
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
        publication, _schedule, attempt, channel, item = row

        publication_ids = normalize_telegram_message_ids(publication.telegram_message_ids)
        attempt_ids = normalize_telegram_message_ids(attempt.telegram_message_ids)
        if not publication_ids or publication_ids != attempt_ids:
            return None
        result_link = normalize_telegram_result_link(publication.result_link)
        if publication.result_link is not None and result_link is None:
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

        return CanonicalPublicationAdminLogPlan(
            publication_id=int(publication.id),
            log_chat_id=int(log_chat_id),
            source_telegram_chat_id=int(channel.tg_chat_id),
            primary_message_id=int(publication_ids[-1]),
            result_link=result_link,
            author_tg_user_id=author_tg_user_id,
            author_username=author_username,
            author_full_name=author_full_name,
        )
