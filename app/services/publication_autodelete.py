from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


class TelegramDeleteProvider(Protocol):
    async def delete_message(self, *, chat_id: int, message_id: int) -> Any: ...

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ) -> Any: ...


class PublicationAutodeleteSyncConflict(RuntimeError):
    """Telegram deletion resolved, but canonical state changed before commit."""

    def __init__(self) -> None:
        super().__init__("canonical autodelete sync conflict")


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteResult:
    publication_id: int
    outcome: Literal["deleted", "already_deleted", "not_due", "ineligible", "retry"]
    message_count: int = 0
    deleted_count: int = 0
    unavailable_count: int = 0
    retryable_count: int = 0


@dataclass(frozen=True, slots=True)
class _Candidate:
    publication_id: int
    channel_id: int
    tg_chat_id: int
    content_item_id: int
    content_revision: int
    schedule_entry_id: int
    runtime_scheduled_at: str
    telegram_message_ids: tuple[int, ...]
    report_enabled: bool
    result_link: str | None


def _safe_mapping(value: Any) -> dict[str, Any]:
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


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _runtime_due_at(runtime: Mapping[str, Any]) -> tuple[datetime | None, str | None]:
    raw = runtime.get("scheduled_at")
    if not isinstance(raw, str):
        return None, None
    text = raw.strip()
    if not text or len(text) > 128:
        return None, None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError, OverflowError):
        return None, None
    due = _utc(parsed)
    return due, due.isoformat()


def _safe_nonrepeat(schedule: ScheduleEntry) -> bool:
    raw_rule = schedule.repeat_rule
    if raw_rule is not None and not isinstance(raw_rule, Mapping):
        return False
    rule = _safe_mapping(raw_rule)
    enabled = rule.get("enabled")
    return enabled is None or enabled is False


def _safe_time_only_options(
    meta: Mapping[str, Any],
    *,
    allow_report: bool = False,
) -> tuple[bool, bool]:
    raw_options = meta.get("runtime_options")
    if raw_options is not None and not isinstance(raw_options, Mapping):
        return False, False
    options = _safe_mapping(raw_options)

    report_flag = options.get("autodelete_report")
    if report_flag is not None and not isinstance(report_flag, bool):
        return False, False
    report_enabled = report_flag is True
    if report_enabled and not allow_report:
        return False, False

    raw_views = options.get("autodelete_views")
    parsed_views = _positive_int(raw_views)
    if raw_views not in (None, False, 0, "0", "") and parsed_views is None:
        return False, False
    if parsed_views is not None:
        return False, False
    return True, report_enabled


def _is_unavailable_delete_error(exc: Exception) -> bool:
    # Provider text is used only for in-memory classification. It is never returned,
    # logged, or persisted at this boundary.
    text = str(exc).lower()
    return (
        "message to delete not found" in text
        or "message_id_invalid" in text
        or "can't be deleted" in text
        or "cannot be deleted" in text
        or "message can't be deleted" in text
    )


class PublicationAutodeleteService:
    """Delete one due canonical-only publication without reading PostTask.

    Runtime workers may opt into best-effort deletion reports after the destructive
    operation has been durably resolved. Views-based deletion remains fail-closed.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: TelegramDeleteProvider,
        allow_report: bool = False,
    ) -> None:
        self.session = session
        self.provider = provider
        self.allow_report = bool(allow_report)

    async def _candidate(
        self,
        publication_id: int,
        *,
        now: datetime,
    ) -> tuple[_Candidate | None, PublicationAutodeleteResult]:
        row = (
            await self.session.execute(
                select(Publication, ScheduleEntry, ContentItem, Channel)
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
                    ContentItem,
                    and_(
                        ContentItem.id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                        ContentItem.kind == "post",
                    ),
                )
                .join(Channel, Channel.id == Publication.channel_id)
                .where(
                    Publication.id == int(publication_id),
                    Publication.legacy_post_task_id.is_(None),
                    Publication.status == "published",
                    ScheduleEntry.status == "completed",
                )
            )
        ).one_or_none()
        if row is None:
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                publication_id=int(publication_id), outcome="ineligible"
            )

        publication, schedule, item, channel = row
        safe_publication_id = int(publication.id)
        safe_channel_id = int(publication.channel_id)
        safe_tg_chat_id = int(channel.tg_chat_id)
        safe_content_item_id = int(item.id)
        safe_content_revision = int(publication.content_revision)
        safe_schedule_entry_id = int(schedule.id)

        meta = _safe_mapping(publication.meta)
        raw_runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
        if raw_runtime is not None and not isinstance(raw_runtime, Mapping):
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                publication_id=safe_publication_id, outcome="ineligible"
            )
        options_safe, report_enabled = _safe_time_only_options(
            meta,
            allow_report=self.allow_report,
        )
        if not options_safe:
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                publication_id=safe_publication_id, outcome="ineligible"
            )

        runtime = _safe_mapping(raw_runtime)
        ids = tuple(normalize_telegram_message_ids(publication.telegram_message_ids))

        deleted_flag = runtime.get("deleted")
        if deleted_flag is not None and not isinstance(deleted_flag, bool):
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                publication_id=safe_publication_id, outcome="ineligible"
            )
        if deleted_flag is True:
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                publication_id=safe_publication_id,
                outcome="already_deleted",
                message_count=len(ids),
            )
        if not _safe_nonrepeat(schedule):
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                publication_id=safe_publication_id, outcome="ineligible"
            )

        due_at, due_token = _runtime_due_at(runtime)
        if due_at is None or due_token is None or not ids:
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                publication_id=safe_publication_id, outcome="ineligible"
            )
        if due_at > now:
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                publication_id=safe_publication_id,
                outcome="not_due",
                message_count=len(ids),
            )

        candidate = _Candidate(
            publication_id=safe_publication_id,
            channel_id=safe_channel_id,
            tg_chat_id=safe_tg_chat_id,
            content_item_id=safe_content_item_id,
            content_revision=safe_content_revision,
            schedule_entry_id=safe_schedule_entry_id,
            runtime_scheduled_at=due_token,
            telegram_message_ids=ids,
            report_enabled=report_enabled,
            result_link=normalize_telegram_result_link(publication.result_link),
        )
        # Never hold a DB transaction open while performing provider network calls.
        await self.session.rollback()
        return candidate, PublicationAutodeleteResult(
            publication_id=candidate.publication_id,
            outcome="retry",
            message_count=len(ids),
        )

    async def _mark_deleted(
        self,
        candidate: _Candidate,
        *,
        deleted_at: datetime,
    ) -> PublicationAutodeleteResult:
        row = (
            await self.session.execute(
                select(Publication, ScheduleEntry, ContentItem)
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
                    ContentItem,
                    and_(
                        ContentItem.id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                        ContentItem.kind == "post",
                    ),
                )
                .where(
                    Publication.id == candidate.publication_id,
                    Publication.legacy_post_task_id.is_(None),
                    Publication.status == "published",
                    Publication.channel_id == candidate.channel_id,
                    Publication.content_item_id == candidate.content_item_id,
                    Publication.content_revision == candidate.content_revision,
                    Publication.schedule_entry_id == candidate.schedule_entry_id,
                    ScheduleEntry.status == "completed",
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()

        publication, schedule, _item = row
        if not _safe_nonrepeat(schedule):
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()
        if tuple(normalize_telegram_message_ids(publication.telegram_message_ids)) != (
            candidate.telegram_message_ids
        ):
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()

        meta = _safe_mapping(publication.meta)
        options_safe, report_enabled = _safe_time_only_options(
            meta,
            allow_report=self.allow_report,
        )
        if not options_safe or report_enabled != candidate.report_enabled:
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()
        raw_runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
        if raw_runtime is not None and not isinstance(raw_runtime, Mapping):
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()
        runtime = _safe_mapping(raw_runtime)
        due_at, due_token = _runtime_due_at(runtime)
        deleted_flag = runtime.get("deleted")
        if deleted_flag is not None and not isinstance(deleted_flag, bool):
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()
        if deleted_flag is True:
            await self.session.rollback()
            return PublicationAutodeleteResult(
                publication_id=candidate.publication_id,
                outcome="already_deleted",
                message_count=len(candidate.telegram_message_ids),
            )
        if due_at is None or due_token != candidate.runtime_scheduled_at:
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()

        runtime["deleted"] = True
        runtime["deleted_at"] = deleted_at.isoformat()
        new_meta = deepcopy(meta)
        new_meta[AUTODELETE_RUNTIME_META_KEY] = runtime
        publication.meta = new_meta
        await self.session.commit()
        return PublicationAutodeleteResult(
            publication_id=candidate.publication_id,
            outcome="deleted",
            message_count=len(candidate.telegram_message_ids),
            deleted_count=len(candidate.telegram_message_ids),
        )

    async def _send_report_best_effort(self, candidate: _Candidate) -> None:
        if not candidate.report_enabled:
            return

        try:
            recipient = (
                await self.session.execute(
                    select(Client.tg_user_id)
                    .join(Channel, Channel.owner_id == Client.id)
                    .where(Channel.id == candidate.channel_id)
                )
            ).scalar_one_or_none()
            # Release the read transaction before the provider network side effect.
            await self.session.rollback()
            if recipient is None:
                return

            text = "🗑️ Пост удалён по таймеру"
            if candidate.result_link:
                text = f"{text}\n{candidate.result_link}"
            await self.provider.send_message(
                chat_id=int(recipient),
                text=text,
                disable_web_page_preview=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await self.session.rollback()
            except Exception:
                pass
            logger.warning(
                "Publication autodelete: report failed publication_id={} type={}",
                candidate.publication_id,
                type(exc).__name__,
            )

    async def delete_if_due(
        self,
        publication_id: int,
        *,
        now: datetime | None = None,
    ) -> PublicationAutodeleteResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return PublicationAutodeleteResult(publication_id=0, outcome="ineligible")
        if safe_publication_id <= 0:
            return PublicationAutodeleteResult(
                publication_id=safe_publication_id, outcome="ineligible"
            )

        current = _utc(now)
        candidate, early = await self._candidate(safe_publication_id, now=current)
        if candidate is None:
            return early

        deleted_count = 0
        unavailable_count = 0
        retryable_count = 0
        for message_id in candidate.telegram_message_ids:
            try:
                await self.provider.delete_message(
                    chat_id=candidate.tg_chat_id,
                    message_id=int(message_id),
                )
                deleted_count += 1
            except Exception as exc:
                if _is_unavailable_delete_error(exc):
                    unavailable_count += 1
                else:
                    retryable_count += 1

        if retryable_count:
            return PublicationAutodeleteResult(
                publication_id=candidate.publication_id,
                outcome="retry",
                message_count=len(candidate.telegram_message_ids),
                deleted_count=deleted_count,
                unavailable_count=unavailable_count,
                retryable_count=retryable_count,
            )

        result = await self._mark_deleted(candidate, deleted_at=current)
        if result.outcome == "deleted" and deleted_count > 0:
            await self._send_report_best_effort(candidate)
        return PublicationAutodeleteResult(
            publication_id=result.publication_id,
            outcome=result.outcome,
            message_count=len(candidate.telegram_message_ids),
            deleted_count=deleted_count,
            unavailable_count=unavailable_count,
            retryable_count=0,
        )