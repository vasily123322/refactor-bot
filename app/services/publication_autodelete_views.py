from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem
from app.domain.models import Channel, Client
from app.domain.publication_autodelete import (
    PublicationAutodeleteLease,
    PublicationAutodeleteViewState,
)
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_views_lifecycle_authority import (
    CanonicalRepeatViewsLifecycleAuthorityService,
)
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle
from app.services.publication_autodelete_views_action_ledger import (
    PublicationAutodeleteViewsActionLedger,
    PublicationAutodeleteViewsActionReservation,
    inspect_publication_autodelete_views_actions,
)
from app.services.publication_mixed_autodelete import (
    PublicationMixedAutodeleteService,
    PublicationMixedAutodeleteSyncConflict,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


class TelegramMessageViewsSource(Protocol):
    async def get_message_views(self, target: str | int, message_id: int) -> int | None: ...


class TelegramDeleteProvider(Protocol):
    async def delete_message(self, *, chat_id: int, message_id: int) -> Any: ...

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ) -> Any: ...


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
    ambiguous_count: int = 0


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
    report_enabled: bool
    result_link: str | None
    authority_fingerprint: str
    ledger_observed_views: int | None = None
    ledger_succeeded_count: int = 0
    ledger_unavailable_count: int = 0


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
    if runtime.get("scheduled_at") is not None or runtime.get("effective_seconds") is not None:
        return False, False
    return True, False


def _view_intent(
    options: Mapping[str, Any],
    *,
    allow_report: bool = False,
) -> tuple[bool, int | None, bool]:
    threshold = _positive_int(options.get("autodelete_views"))
    raw_threshold = options.get("autodelete_views")
    if raw_threshold not in (None, False, 0, "0", "") and threshold is None:
        return False, None, False

    seconds = _positive_int(options.get("autodelete_seconds"))
    raw_seconds = options.get("autodelete_seconds")
    if raw_seconds not in (None, False, 0, "0", "") and seconds is None:
        return False, None, False
    if seconds is not None:
        return False, None, False

    report = options.get("autodelete_report")
    if report is not None and not isinstance(report, bool):
        return False, None, False
    report_enabled = report is True
    if report_enabled and not allow_report:
        return False, None, False
    return threshold is not None, threshold, report_enabled


def _is_unavailable_delete_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "message to delete not found" in text
        or "message_id_invalid" in text
        or "can't be deleted" in text
        or "cannot be deleted" in text
        or "message can't be deleted" in text
    )


def _fingerprint(payload: Mapping[str, Any]) -> str | None:
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    return hashlib.sha256(encoded).hexdigest()


class PublicationAutodeleteViewsService:
    """Evaluate views and delete through a durable reserve-before-provider barrier.

    The publication autodelete lease serializes workers but is not itself destructive
    authority. Every Telegram DELETE requires a newly committed occurrence-local action
    reservation bound to exact current Publication/Schedule/Channel/views lifecycle
    authority and the exact live lease token+holder. Provider calls use only the immutable
    chat/message captured by that reservation and execute after commit, outside the DB
    transaction.

    Existing ``reserved``/``unknown`` actions are permanent automatic no-replay barriers.
    Terminal ``succeeded``/``unavailable`` actions may be reused only as evidence for the
    exact same authority fingerprint, allowing clean per-message continuation without
    replay. Repeat views remain independently default-off.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        view_source: TelegramMessageViewsSource,
        delete_provider: TelegramDeleteProvider,
        next_check_seconds: int = 60,
        allow_report: bool = False,
        allow_repeat_views: bool = False,
        lease: PublicationAutodeleteLeaseHandle | None = None,
    ) -> None:
        self.session = session
        self.view_source = view_source
        self.delete_provider = delete_provider
        self.next_check_seconds = max(15, min(int(next_check_seconds), 3600))
        self.allow_report = bool(allow_report)
        self.allow_repeat_views = bool(allow_repeat_views)
        self.lease = lease

    async def _authoritative_intent(
        self,
        publication: Publication,
        *,
        telegram_message_ids: tuple[int, ...],
    ) -> tuple[bool, int | None, bool]:
        meta = _mapping(publication.meta)
        if meta is None:
            return False, None, False
        raw_options = meta.get("runtime_options")
        if raw_options is not None and not isinstance(raw_options, Mapping):
            return False, None, False
        options = _mapping(raw_options)
        if options is None:
            return False, None, False
        return _view_intent(options, allow_report=self.allow_report)

    async def _lifecycle_allowed(
        self,
        publication: Publication,
        schedule: ScheduleEntry,
        *,
        telegram_message_ids: tuple[int, ...],
        threshold: int,
        report_enabled: bool,
    ) -> bool:
        if _safe_nonrepeat(schedule):
            return True
        if not self.allow_repeat_views:
            return False

        proof = await CanonicalRepeatViewsLifecycleAuthorityService(
            self.session
        ).lock_and_prove(int(publication.id))
        if proof is None:
            return False
        return (
            proof.publication_id == int(publication.id)
            and proof.schedule_entry_id == int(schedule.id)
            and proof.threshold == int(threshold)
            and proof.autodelete_report == bool(report_enabled)
            and proof.telegram_message_ids == telegram_message_ids
        )

    def _authority_fingerprint(
        self,
        *,
        publication: Publication,
        schedule: ScheduleEntry,
        channel: Channel,
        telegram_message_ids: tuple[int, ...],
        threshold: int,
        report_enabled: bool,
    ) -> str | None:
        meta = _mapping(publication.meta)
        repeat_rule = _mapping(schedule.repeat_rule)
        if meta is None or repeat_rule is None:
            return None
        raw_options = meta.get("runtime_options")
        if raw_options is not None and not isinstance(raw_options, Mapping):
            return None
        runtime_options = _mapping(raw_options)
        if runtime_options is None:
            return None
        return _fingerprint(
            {
                "version": 1,
                "publication_id": int(publication.id),
                "channel_id": int(publication.channel_id),
                "telegram_chat_id": int(channel.tg_chat_id),
                "content_item_id": int(publication.content_item_id),
                "content_revision": int(publication.content_revision),
                "schedule_entry_id": int(schedule.id),
                "schedule_scheduled_at": _utc(schedule.scheduled_at).isoformat(),
                "schedule_timezone": schedule.timezone,
                "legacy_post_task_id": (
                    int(publication.legacy_post_task_id)
                    if publication.legacy_post_task_id is not None
                    else None
                ),
                "threshold": int(threshold),
                "report_enabled": bool(report_enabled),
                "telegram_message_ids": list(telegram_message_ids),
                "result_link": normalize_telegram_result_link(publication.result_link),
                "repeat_rule": repeat_rule,
                "runtime_options": runtime_options,
            }
        )

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
        intent_safe, authoritative_threshold, report_enabled = (
            await self._authoritative_intent(
                publication,
                telegram_message_ids=ids,
            )
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
        if not await self._lifecycle_allowed(
            publication,
            schedule,
            telegram_message_ids=ids,
            threshold=threshold,
            report_enabled=report_enabled,
        ):
            await self.session.rollback()
            return None, PublicationAutodeleteViewsResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
                threshold=threshold,
                message_count=len(ids),
            )

        fingerprint = self._authority_fingerprint(
            publication=publication,
            schedule=schedule,
            channel=channel,
            telegram_message_ids=ids,
            threshold=threshold,
            report_enabled=report_enabled,
        )
        if fingerprint is None or meta is None:
            await self.session.rollback()
            return None, PublicationAutodeleteViewsResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
                threshold=threshold,
                message_count=len(ids),
            )

        inspection = inspect_publication_autodelete_views_actions(
            meta,
            authority_fingerprint=fingerprint,
            telegram_chat_id=int(channel.tg_chat_id),
            telegram_message_ids=ids,
            threshold=threshold,
        )
        if inspection.outcome == "conflict":
            await self.session.rollback()
            raise PublicationAutodeleteViewsSyncConflict()
        if inspection.outcome == "ambiguous":
            await self.session.rollback()
            return None, PublicationAutodeleteViewsResult(
                publication_id=safe_publication_id,
                outcome="retry",
                threshold=threshold,
                observed_views=inspection.observed_views,
                message_count=len(ids),
                deleted_count=inspection.succeeded_count,
                unavailable_count=inspection.unavailable_count,
                ambiguous_count=1,
            )

        next_check_at = _utc(state.next_check_at)
        if inspection.action_count == 0 and next_check_at > now:
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
            report_enabled=report_enabled,
            result_link=normalize_telegram_result_link(publication.result_link),
            authority_fingerprint=fingerprint,
            ledger_observed_views=(
                inspection.observed_views if inspection.action_count > 0 else None
            ),
            ledger_succeeded_count=inspection.succeeded_count,
            ledger_unavailable_count=inspection.unavailable_count,
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
    ) -> tuple[
        Publication,
        PublicationAutodeleteViewState | None,
        ScheduleEntry,
        Channel,
    ] | Literal["already_deleted"]:
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

        schedule = (
            await self.session.execute(
                select(ScheduleEntry)
                .where(ScheduleEntry.id == candidate.schedule_entry_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        item = (
            await self.session.execute(
                select(ContentItem)
                .where(ContentItem.id == candidate.content_item_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        channel = (
            await self.session.execute(
                select(Channel)
                .where(Channel.id == candidate.channel_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            schedule is None
            or str(schedule.status) != "completed"
            or int(schedule.channel_id) != candidate.channel_id
            or int(schedule.content_item_id) != candidate.content_item_id
            or int(schedule.content_revision) != candidate.content_revision
            or item is None
            or str(item.kind) != "post"
            or int(item.channel_id) != candidate.channel_id
            or channel is None
            or int(channel.tg_chat_id) != candidate.tg_chat_id
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

        intent_safe, threshold, report_enabled = await self._authoritative_intent(
            publication,
            telegram_message_ids=ids,
        )
        if (
            not intent_safe
            or threshold != candidate.threshold
            or report_enabled != candidate.report_enabled
        ):
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
        if not await self._lifecycle_allowed(
            publication,
            schedule,
            telegram_message_ids=ids,
            threshold=candidate.threshold,
            report_enabled=candidate.report_enabled,
        ):
            raise PublicationAutodeleteViewsSyncConflict()

        fingerprint = self._authority_fingerprint(
            publication=publication,
            schedule=schedule,
            channel=channel,
            telegram_message_ids=ids,
            threshold=candidate.threshold,
            report_enabled=candidate.report_enabled,
        )
        if fingerprint != candidate.authority_fingerprint:
            raise PublicationAutodeleteViewsSyncConflict()
        return publication, state, schedule, channel

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

    async def _reserve_message(
        self,
        candidate: _Candidate,
        *,
        message_id: int,
        observed_views: int,
    ):
        handle = self.lease
        if handle is None or int(handle.publication_id) != candidate.publication_id:
            return "ineligible", None

        current = await self._load_current(candidate, require_state=True)
        if current == "already_deleted":
            await self.session.rollback()
            await self._cleanup_terminal_state(candidate.publication_id)
            return "already_deleted", None

        result = await PublicationAutodeleteViewsActionLedger(self.session).reserve(
            handle,
            telegram_chat_id=candidate.tg_chat_id,
            telegram_message_id=int(message_id),
            authority_fingerprint=candidate.authority_fingerprint,
            telegram_message_ids=candidate.telegram_message_ids,
            threshold=candidate.threshold,
            observed_views=observed_views,
            now=_utc(),
        )
        return result.outcome, result

    async def _mark_action(
        self,
        reservation: PublicationAutodeleteViewsActionReservation,
        state: Literal["succeeded", "unavailable", "unknown"],
    ) -> bool:
        ledger = PublicationAutodeleteViewsActionLedger(self.session)
        if state == "succeeded":
            return await ledger.mark_succeeded(reservation)
        if state == "unavailable":
            return await ledger.mark_unavailable(reservation)
        return await ledger.mark_unknown(reservation)

    async def _best_effort_mark_action(
        self,
        reservation: PublicationAutodeleteViewsActionReservation,
        state: Literal["succeeded", "unavailable", "unknown"],
    ) -> bool:
        try:
            return await self._mark_action(reservation, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Publication views autodelete action mark failed publication_id={} "
                "message_id={} state={} type={}",
                int(reservation.publication_id),
                int(reservation.telegram_message_id),
                state,
                type(exc).__name__,
            )
            return False

    async def _live_lease(self, *, at: datetime) -> bool:
        handle = self.lease
        if handle is None:
            return False
        lease = (
            await self.session.execute(
                select(PublicationAutodeleteLease)
                .where(
                    PublicationAutodeleteLease.publication_id
                    == int(handle.publication_id),
                    PublicationAutodeleteLease.lease_token == str(handle.lease_token),
                    PublicationAutodeleteLease.holder == str(handle.holder),
                    PublicationAutodeleteLease.expires_at > at,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return lease is not None

    async def _mark_deleted(
        self,
        candidate: _Candidate,
        *,
        observed_views: int,
        deleted_at: datetime,
    ) -> PublicationAutodeleteViewsResult:
        current = await self._load_current(candidate, require_state=True)
        if current == "already_deleted":
            await self.session.rollback()
            await self._cleanup_terminal_state(candidate.publication_id)
            return PublicationAutodeleteViewsResult(
                publication_id=candidate.publication_id,
                outcome="already_deleted",
                threshold=candidate.threshold,
                observed_views=observed_views,
                message_count=len(candidate.telegram_message_ids),
            )
        publication, state, _, _ = current
        assert state is not None

        if not await self._live_lease(at=_utc()):
            await self.session.rollback()
            return PublicationAutodeleteViewsResult(
                publication_id=candidate.publication_id,
                outcome="retry",
                threshold=candidate.threshold,
                observed_views=observed_views,
                message_count=len(candidate.telegram_message_ids),
            )

        meta = _mapping(publication.meta)
        if meta is None:
            await self.session.rollback()
            raise PublicationAutodeleteViewsSyncConflict()
        inspection = inspect_publication_autodelete_views_actions(
            meta,
            authority_fingerprint=candidate.authority_fingerprint,
            telegram_chat_id=candidate.tg_chat_id,
            telegram_message_ids=candidate.telegram_message_ids,
            threshold=candidate.threshold,
        )
        if inspection.outcome == "ambiguous":
            await self.session.rollback()
            return PublicationAutodeleteViewsResult(
                publication_id=candidate.publication_id,
                outcome="retry",
                threshold=candidate.threshold,
                observed_views=inspection.observed_views,
                message_count=len(candidate.telegram_message_ids),
                deleted_count=inspection.succeeded_count,
                unavailable_count=inspection.unavailable_count,
                ambiguous_count=1,
            )
        if (
            inspection.outcome != "clean"
            or inspection.observed_views != observed_views
            or inspection.action_count != len(candidate.telegram_message_ids)
            or (
                inspection.succeeded_count + inspection.unavailable_count
                != len(candidate.telegram_message_ids)
            )
        ):
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
        return PublicationAutodeleteViewsResult(
            publication_id=candidate.publication_id,
            outcome="deleted",
            threshold=candidate.threshold,
            observed_views=observed_views,
            message_count=len(candidate.telegram_message_ids),
            deleted_count=inspection.succeeded_count,
            unavailable_count=inspection.unavailable_count,
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
            await self.session.rollback()
            if recipient is None:
                return

            text = "🗑️ Пост удалён по просмотрам"
            if candidate.result_link:
                text = f"{text}\n{candidate.result_link}"
            await self.delete_provider.send_message(
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
                "Publication views autodelete: report failed publication_id={} type={}",
                candidate.publication_id,
                type(exc).__name__,
            )

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

        if self.lease is not None:
            try:
                mixed = await PublicationMixedAutodeleteService(
                    self.session,
                    view_source=self.view_source,
                    delete_provider=self.delete_provider,
                    next_check_seconds=self.next_check_seconds,
                    allow_report=self.allow_report,
                ).views_evaluate_and_delete(
                    safe_publication_id,
                    lease=self.lease,
                    now=now,
                )
            except PublicationMixedAutodeleteSyncConflict as exc:
                raise PublicationAutodeleteViewsSyncConflict() from exc
            if mixed is not None:
                outcome = (
                    mixed.outcome
                    if mixed.outcome
                    in {
                        "deleted",
                        "already_deleted",
                        "below_threshold",
                        "deferred",
                        "not_due",
                        "ineligible",
                        "retry",
                    }
                    else "retry"
                )
                return PublicationAutodeleteViewsResult(
                    publication_id=mixed.publication_id,
                    outcome=outcome,
                    threshold=mixed.threshold,
                    observed_views=mixed.observed_views,
                    message_count=mixed.message_count,
                    deleted_count=mixed.deleted_count,
                    unavailable_count=mixed.unavailable_count,
                    ambiguous_count=mixed.ambiguous_count,
                )

        current = _utc(now)
        candidate, early = await self._candidate(safe_publication_id, now=current)
        if candidate is None:
            return early

        if candidate.ledger_observed_views is None:
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
        else:
            observed_views = int(candidate.ledger_observed_views)

        if self.lease is None or int(self.lease.publication_id) != candidate.publication_id:
            return PublicationAutodeleteViewsResult(
                publication_id=candidate.publication_id,
                outcome="ineligible",
                threshold=candidate.threshold,
                observed_views=observed_views,
                message_count=len(candidate.telegram_message_ids),
            )

        deleted_count = candidate.ledger_succeeded_count
        unavailable_count = candidate.ledger_unavailable_count
        for message_id in candidate.telegram_message_ids:
            outcome, reserve_result = await self._reserve_message(
                candidate,
                message_id=int(message_id),
                observed_views=observed_views,
            )
            if outcome == "already_deleted":
                return PublicationAutodeleteViewsResult(
                    publication_id=candidate.publication_id,
                    outcome="already_deleted",
                    threshold=candidate.threshold,
                    observed_views=observed_views,
                    message_count=len(candidate.telegram_message_ids),
                )
            if outcome == "conflict":
                raise PublicationAutodeleteViewsSyncConflict()
            if outcome == "ineligible":
                return PublicationAutodeleteViewsResult(
                    publication_id=candidate.publication_id,
                    outcome="retry",
                    threshold=candidate.threshold,
                    observed_views=observed_views,
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                )
            if outcome == "ambiguous":
                return PublicationAutodeleteViewsResult(
                    publication_id=candidate.publication_id,
                    outcome="retry",
                    threshold=candidate.threshold,
                    observed_views=observed_views,
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                )
            if reserve_result is None:
                raise PublicationAutodeleteViewsSyncConflict()
            if outcome == "already_terminal":
                state = str(reserve_result.existing_state)
                if candidate.ledger_observed_views is None:
                    if state == "succeeded":
                        deleted_count += 1
                    elif state == "unavailable":
                        unavailable_count += 1
                    else:
                        raise PublicationAutodeleteViewsSyncConflict()
                continue

            reservation = reserve_result.reservation
            if reservation is None:
                raise PublicationAutodeleteViewsSyncConflict()
            try:
                await self.delete_provider.delete_message(
                    chat_id=int(reservation.telegram_chat_id),
                    message_id=int(reservation.telegram_message_id),
                )
            except asyncio.CancelledError:
                try:
                    await self._best_effort_mark_action(reservation, "unknown")
                finally:
                    raise
            except Exception as exc:
                if _is_unavailable_delete_error(exc):
                    marked = await self._best_effort_mark_action(
                        reservation,
                        "unavailable",
                    )
                    if not marked:
                        return PublicationAutodeleteViewsResult(
                            publication_id=candidate.publication_id,
                            outcome="retry",
                            threshold=candidate.threshold,
                            observed_views=observed_views,
                            message_count=len(candidate.telegram_message_ids),
                            deleted_count=deleted_count,
                            unavailable_count=unavailable_count,
                            ambiguous_count=1,
                        )
                    unavailable_count += 1
                    continue

                await self._best_effort_mark_action(reservation, "unknown")
                return PublicationAutodeleteViewsResult(
                    publication_id=candidate.publication_id,
                    outcome="retry",
                    threshold=candidate.threshold,
                    observed_views=observed_views,
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                )

            marked = await self._best_effort_mark_action(reservation, "succeeded")
            if not marked:
                return PublicationAutodeleteViewsResult(
                    publication_id=candidate.publication_id,
                    outcome="retry",
                    threshold=candidate.threshold,
                    observed_views=observed_views,
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                )
            deleted_count += 1

        result = await self._mark_deleted(
            candidate,
            observed_views=observed_views,
            deleted_at=current,
        )
        if result.outcome == "deleted" and result.deleted_count > 0:
            await self._send_report_best_effort(candidate)
        return result
