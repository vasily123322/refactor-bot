from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem
from app.domain.models import Channel, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import normalize_telegram_message_ids


class TelegramMessageViewsSource(Protocol):
    async def get_message_views(self, target: str | int, message_id: int) -> int | None: ...


class TelegramDeleteProvider(Protocol):
    async def delete_message(self, *, chat_id: int, message_id: int) -> Any: ...


class PublicationAutodeleteViewsSyncConflict(RuntimeError):
    """External observations/side effects resolved against stale canonical state."""

    def __init__(self) -> None:
        super().__init__("canonical views autodelete sync conflict")


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewsResult:
    publication_id: int
    outcome: Literal[
        "deleted",
        "already_deleted",
        "below_threshold",
        "deferred",
        "not_due",
        "ineligible",
        "retry",
    ]
    threshold: int | None = None
    observed_views: int | None = None
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
    legacy_post_task_id: int | None
    threshold: int
    telegram_message_ids: tuple[int, ...]


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return {}
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


def _nonnegative_view_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value) if int(value) >= 0 else None


def _safe_nonrepeat(schedule: ScheduleEntry) -> bool:
    raw_rule = schedule.repeat_rule
    if raw_rule is not None and not isinstance(raw_rule, Mapping):
        return False
    rule = _mapping(raw_rule)
    if rule is None:
        return False
    enabled = rule.get("enabled")
    return enabled is None or enabled is False


def _runtime_status(meta: Mapping[str, Any]) -> tuple[bool, bool]:
    raw = meta.get(AUTODELETE_RUNTIME_META_KEY)
    if raw is None:
        return True, False
    if not isinstance(raw, Mapping):
        return False, False
    runtime = _mapping(raw)
    if runtime is None:
        return False, False
    deleted = runtime.get("deleted")
    if deleted is not None and not isinstance(deleted, bool):
        return False, False
    if deleted is True:
        return True, True
    # A pending time-based runtime must never be repurposed as a views runtime.
    if runtime.get("scheduled_at") is not None or runtime.get("effective_seconds") is not None:
        return False, False
    return True, False


def _view_intent(options: Mapping[str, Any]) -> tuple[bool, int | None]:
    threshold = _positive_int(options.get("autodelete_views"))
    raw_threshold = options.get("autodelete_views")
    if raw_threshold not in (None, False, 0, "0", "") and threshold is None:
        return False, None

    seconds = _positive_int(options.get("autodelete_seconds"))
    raw_seconds = options.get("autodelete_seconds")
    if raw_seconds not in (None, False, 0, "0", "") and seconds is None:
        return False, None
    if seconds is not None:
        return False, None

    report = options.get("autodelete_report")
    if report is not None and not isinstance(report, bool):
        return False, None
    # Report parity is intentionally a later stage. Do not silently drop it.
    if report is True:
        return False, None
    return threshold is not None, threshold


def _is_unavailable_delete_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "message to delete not found" in text
        or "message_id_invalid" in text
        or "can't be deleted" in text
        or "cannot be deleted" in text
        or "message can't be deleted" in text
    )


class PublicationAutodeleteViewsService:
    """Evaluate and delete one views-based Publication with fail-closed revalidation.

    This service does not own distributed scheduling. A later worker must acquire the
    existing Publication autodelete lease before calling it. Network calls are never
    made with a DB transaction held open.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        view_source: TelegramMessageViewsSource,
        delete_provider: TelegramDeleteProvider,
        next_check_seconds: int = 60,
    ) -> None:
        self.session = session
        self.view_source = view_source
        self.delete_provider = delete_provider
        self.next_check_seconds = max(15, min(int(next_check_seconds), 3600))

    async def _authoritative_threshold(
        self,
        publication: Publication,
        *,
        telegram_message_ids: tuple[int, ...],
    ) -> tuple[bool, int | None]:
        raw_legacy_id = publication.legacy_post_task_id
        if raw_legacy_id is not None:
            try:
                legacy_id = int(raw_legacy_id)
            except (TypeError, ValueError, OverflowError):
                return False, None
            task = await self.session.get(PostTask, legacy_id)
            if task is None:
                return False, None
            if int(task.channel_id) != int(publication.channel_id) or str(task.status) != "done":
                return False, None
            payload = _mapping(task.payload)
            if payload is None:
                return False, None
            if tuple(normalize_telegram_message_ids(payload.get("result_ids"))) != telegram_message_ids:
                return False, None
            return _view_intent(payload)

        meta = _mapping(publication.meta)
        if meta is None:
            return False, None
        raw_options = meta.get("runtime_options")
        if raw_options is not None and not isinstance(raw_options, Mapping):
            return False, None
        options = _mapping(raw_options)
        if options is None:
            return False, None
        return _view_intent(options)

    async def _candidate(
        self,
        publication_id: int,
        *,
        now: datetime,
    ) -> tuple[_Candidate | None, PublicationAutodeleteViewsResult]:
        row = (
            await self.session.execute(
                select(
                    Publication,
                    ScheduleEntry,
                    ContentItem,
                    Channel,
                    PublicationAutodeleteViewState,
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
                    ContentItem,
                    and_(
                        ContentItem.id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                        ContentItem.kind == "post",
                    ),
                )
                .join(Channel, Channel.id == Publication.channel_id)
                .join(
                    PublicationAutodeleteViewState,
                    PublicationAutodeleteViewState.publication_id == Publication.id,
                )
                .where(
                    Publication.id == int(publication_id),
                    Publication.status == "published",
                    ScheduleEntry.status == "completed",
                )
            )
        ).one_or_none()
        if row is None:
            await self.session.rollback()
            return None, PublicationAutodeleteViewsResult(
                publication_id=int(publication_id), outcome="ineligible"
            )

        publication, schedule, item, channel, state = row
        ids = tuple(normalize_telegram_message_ids(publication.telegram_message_ids))
        threshold = _positive_int(state.threshold)
        meta = _mapping(publication.meta)
        runtime_safe, already_deleted = (
            _runtime_status(meta) if meta is not None else (False, False)
        )
        intent_safe, authoritative_threshold = await self._authoritative_threshold(
            publication,
            telegram_message_ids=ids,
        )
        safe_publication_id = int(publication.id)

        if already_deleted:
            await self.session.rollback()
            await self._cleanup_terminal_state(safe_publication_id)
            return None, PublicationAutodeleteViewsResult(
                publication_id=safe_publication_id,
                outcome="already_deleted",
                threshold=threshold,
                message_count=len(ids),
            )
        if (
            not runtime_safe
            or not _safe_nonrepeat(schedule)
            or not ids
            or threshold is None
            or not intent_safe
            or authoritative_threshold != threshold
        ):
            await self.session.rollback()
            return None, PublicationAutodeleteViewsResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
                threshold=threshold,
                message_count=len(ids),
            )

        next_check_at = _utc(state.next_check_at)
        if next_check_at > now:
            await self.session.rollback()
            return None, PublicationAutodeleteViewsResult(
                publication_id=safe_publication_id,
                outcome="not_due",
                threshold=threshold,
                message_count=len(ids),
            )

        candidate = _Candidate(
            publication_id=safe_publication_id,
            channel_id=int(publication.channel_id),
            tg_chat_id=int(channel.tg_chat_id),
            content_item_id=int(item.id),
            content_revision=int(publication.content_revision),
            schedule_entry_id=int(schedule.id),
            legacy_post_task_id=(
                int(publication.legacy_post_task_id)
                if publication.legacy_post_task_id is not None
                else None
            ),
            threshold=threshold,
            telegram_message_ids=ids,
        )
        await self.session.rollback()
        return candidate, PublicationAutodeleteViewsResult(
            publication_id=candidate.publication_id,
            outcome="deferred",
            threshold=candidate.threshold,
            message_count=len(candidate.telegram_message_ids),
        )

    async def _load_current(
        self,
        candidate: _Candidate,
        *,
        require_state: bool,
    ) -> tuple[Publication, PublicationAutodeleteViewState | None] | Literal["already_deleted"]:
        publication = (
            await self.session.execute(
                select(Publication)
                .where(
                    Publication.id == candidate.publication_id,
                    Publication.status == "published",
                    Publication.channel_id == candidate.channel_id,
                    Publication.content_item_id == candidate.content_item_id,
                    Publication.content_revision == candidate.content_revision,
                    Publication.schedule_entry_id == candidate.schedule_entry_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if publication is None:
            raise PublicationAutodeleteViewsSyncConflict()

        schedule = await self.session.get(ScheduleEntry, candidate.schedule_entry_id)
        item = await self.session.get(ContentItem, candidate.content_item_id)
        if (
            schedule is None
            or str(schedule.status) != "completed"
            or int(schedule.channel_id) != candidate.channel_id
            or int(schedule.content_item_id) != candidate.content_item_id
            or int(schedule.content_revision) != candidate.content_revision
            or not _safe_nonrepeat(schedule)
            or item is None
            or str(item.kind) != "post"
            or int(item.channel_id) != candidate.channel_id
        ):
            raise PublicationAutodeleteViewsSyncConflict()

        ids = tuple(normalize_telegram_message_ids(publication.telegram_message_ids))
        if ids != candidate.telegram_message_ids:
            raise PublicationAutodeleteViewsSyncConflict()
        current_legacy_id = (
            int(publication.legacy_post_task_id)
            if publication.legacy_post_task_id is not None
            else None
        )
        if current_legacy_id != candidate.legacy_post_task_id:
            raise PublicationAutodeleteViewsSyncConflict()

        meta = _mapping(publication.meta)
        runtime_safe, already_deleted = (
            _runtime_status(meta) if meta is not None else (False, False)
        )
        if not runtime_safe:
            raise PublicationAutodeleteViewsSyncConflict()
        if already_deleted:
            return "already_deleted"

        intent_safe, threshold = await self._authoritative_threshold(
            publication,
            telegram_message_ids=ids,
        )
        if not intent_safe or threshold != candidate.threshold:
            raise PublicationAutodeleteViewsSyncConflict()

        state = (
            await self.session.execute(
                select(PublicationAutodeleteViewState)
                .where(
                    PublicationAutodeleteViewState.publication_id
                    == candidate.publication_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if require_state and (state is None or int(state.threshold) != candidate.threshold):
            raise PublicationAutodeleteViewsSyncConflict()
        if state is not None and int(state.threshold) != candidate.threshold:
            raise PublicationAutodeleteViewsSyncConflict()
        return publication, state

    async def _cleanup_terminal_state(self, publication_id: int) -> None:
        publication = (
            await self.session.execute(
                select(Publication)
                .where(Publication.id == int(publication_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if publication is None:
            await self.session.rollback()
            return
        meta = _mapping(publication.meta)
        runtime_safe, already_deleted = (
            _runtime_status(meta) if meta is not None else (False, False)
        )
        if not runtime_safe or not already_deleted:
            await self.session.rollback()
            return
        state = await self.session.get(PublicationAutodeleteViewState, int(publication_id))
        if state is not None:
            await self.session.delete(state)
        await self.session.commit()

    async def _defer(
        self,
        candidate: _Candidate,
        *,
        now: datetime,
        observed_views: int | None = None,
    ) -> None:
        state = (
            await self.session.execute(
                select(PublicationAutodeleteViewState)
                .where(
                    PublicationAutodeleteViewState.publication_id
                    == candidate.publication_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state is None or int(state.threshold) != candidate.threshold:
            await self.session.rollback()
            raise PublicationAutodeleteViewsSyncConflict()
        state.next_check_at = now + timedelta(seconds=self.next_check_seconds)
        if observed_views is not None:
            state.last_views = int(observed_views)
            state.last_checked_at = now
        await self.session.commit()

    async def _predelete_revalidate(self, candidate: _Candidate) -> bool:
        current = await self._load_current(candidate, require_state=True)
        if current == "already_deleted":
            await self.session.rollback()
            await self._cleanup_terminal_state(candidate.publication_id)
            return False
        await self.session.rollback()
        return True

    async def _mark_deleted(
        self,
        candidate: _Candidate,
        *,
        observed_views: int,
        deleted_at: datetime,
    ) -> Literal["deleted", "already_deleted"]:
        current = await self._load_current(candidate, require_state=True)
        if current == "already_deleted":
            await self.session.rollback()
            await self._cleanup_terminal_state(candidate.publication_id)
            return "already_deleted"
        publication, state = current
        assert state is not None

        meta = _mapping(publication.meta)
        if meta is None:
            await self.session.rollback()
            raise PublicationAutodeleteViewsSyncConflict()
        runtime = {
            "mode": "views",
            "view_threshold": int(candidate.threshold),
            "observed_views": int(observed_views),
            "deleted": True,
            "deleted_at": deleted_at.isoformat(),
        }
        new_meta = deepcopy(meta)
        new_meta[AUTODELETE_RUNTIME_META_KEY] = runtime
        publication.meta = new_meta
        await self.session.delete(state)
        await self.session.commit()
        return "deleted"

    async def evaluate_and_delete(
        self,
        publication_id: int,
        *,
        now: datetime | None = None,
    ) -> PublicationAutodeleteViewsResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return PublicationAutodeleteViewsResult(publication_id=0, outcome="ineligible")
        if safe_publication_id <= 0:
            return PublicationAutodeleteViewsResult(
                publication_id=safe_publication_id, outcome="ineligible"
            )

        current = _utc(now)
        candidate, early = await self._candidate(safe_publication_id, now=current)
        if candidate is None:
            return early

        counts: list[int] = []
        for message_id in candidate.telegram_message_ids:
            try:
                raw_views = await self.view_source.get_message_views(
                    candidate.tg_chat_id,
                    int(message_id),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._defer(candidate, now=current)
                return PublicationAutodeleteViewsResult(
                    publication_id=candidate.publication_id,
                    outcome="deferred",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                )
            views = _nonnegative_view_count(raw_views)
            if views is None:
                await self._defer(candidate, now=current)
                return PublicationAutodeleteViewsResult(
                    publication_id=candidate.publication_id,
                    outcome="deferred",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                )
            counts.append(views)

        observed_views = min(counts)
        if observed_views < candidate.threshold:
            await self._defer(
                candidate,
                now=current,
                observed_views=observed_views,
            )
            return PublicationAutodeleteViewsResult(
                publication_id=candidate.publication_id,
                outcome="below_threshold",
                threshold=candidate.threshold,
                observed_views=observed_views,
                message_count=len(candidate.telegram_message_ids),
            )

        if not await self._predelete_revalidate(candidate):
            return PublicationAutodeleteViewsResult(
                publication_id=candidate.publication_id,
                outcome="already_deleted",
                threshold=candidate.threshold,
                observed_views=observed_views,
                message_count=len(candidate.telegram_message_ids),
            )

        deleted_count = 0
        unavailable_count = 0
        retryable_count = 0
        for message_id in candidate.telegram_message_ids:
            try:
                await self.delete_provider.delete_message(
                    chat_id=candidate.tg_chat_id,
                    message_id=int(message_id),
                )
                deleted_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_unavailable_delete_error(exc):
                    unavailable_count += 1
                else:
                    retryable_count += 1

        if retryable_count:
            await self._defer(candidate, now=current)
            return PublicationAutodeleteViewsResult(
                publication_id=candidate.publication_id,
                outcome="retry",
                threshold=candidate.threshold,
                observed_views=observed_views,
                message_count=len(candidate.telegram_message_ids),
                deleted_count=deleted_count,
                unavailable_count=unavailable_count,
                retryable_count=retryable_count,
            )

        outcome = await self._mark_deleted(
            candidate,
            observed_views=observed_views,
            deleted_at=current,
        )
        return PublicationAutodeleteViewsResult(
            publication_id=candidate.publication_id,
            outcome=outcome,
            threshold=candidate.threshold,
            observed_views=observed_views,
            message_count=len(candidate.telegram_message_ids),
            deleted_count=deleted_count,
            unavailable_count=unavailable_count,
            retryable_count=0,
        )
