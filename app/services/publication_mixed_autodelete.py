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
    PublicationAutodeleteAction,
    PublicationAutodeleteLease,
    PublicationAutodeleteViewState,
)
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_autodelete_action_ledger import (
    PublicationAutodeleteActionLedger,
    PublicationAutodeleteActionReservation,
)
from app.services.publication_autodelete_lease import (
    PublicationAutodeleteLeaseHandle,
    PublicationAutodeleteLeaseService,
)
from app.services.publication_autodelete_views_action_ledger import (
    AUTODELETE_VIEWS_ACTIONS_META_KEY,
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


class PublicationMixedAutodeleteSyncConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("canonical mixed autodelete sync conflict")


@dataclass(frozen=True, slots=True)
class PublicationMixedAutodeleteResult:
    publication_id: int
    outcome: Literal[
        "deleted",
        "already_deleted",
        "not_due",
        "below_threshold",
        "deferred",
        "ineligible",
        "retry",
        "ambiguous",
    ]
    threshold: int | None = None
    observed_views: int | None = None
    message_count: int = 0
    deleted_count: int = 0
    unavailable_count: int = 0
    ambiguous_count: int = 0


@dataclass(frozen=True, slots=True)
class _Candidate:
    publication_id: int
    channel_id: int
    tg_chat_id: int
    content_item_id: int
    content_revision: int
    schedule_entry_id: int
    due_at: datetime
    runtime_scheduled_at: str
    effective_seconds: int
    threshold: int
    telegram_message_ids: tuple[int, ...]
    report_enabled: bool
    result_link: str | None
    authority_fingerprint: str
    next_check_at: datetime


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


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value) if int(value) >= 0 else None


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
    raw = schedule.repeat_rule
    if raw is not None and not isinstance(raw, Mapping):
        return False
    rule = _mapping(raw)
    if rule is None:
        return False
    enabled = rule.get("enabled")
    return enabled is None or enabled is False


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


def _is_unavailable_delete_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "message to delete not found" in text
        or "message_id_invalid" in text
        or "can't be deleted" in text
        or "cannot be deleted" in text
        or "message can't be deleted" in text
    )


def _mixed_intent(
    meta: Mapping[str, Any],
    *,
    allow_report: bool,
) -> tuple[Literal["mixed", "not_mixed", "invalid"], int | None, int | None, bool]:
    raw_options = meta.get("runtime_options")
    if raw_options is not None and not isinstance(raw_options, Mapping):
        return "invalid", None, None, False
    options = _mapping(raw_options)
    if options is None:
        return "invalid", None, None, False

    raw_seconds = options.get("autodelete_seconds")
    raw_views = options.get("autodelete_views")
    seconds = _positive_int(raw_seconds)
    views = _positive_int(raw_views)
    time_requested = raw_seconds not in (None, False, 0, "0", "")
    views_requested = raw_views not in (None, False, 0, "0", "")

    if time_requested and seconds is None:
        return "invalid", None, None, False
    if views_requested and views is None:
        return "invalid", None, None, False
    if seconds is None or views is None:
        return "not_mixed", seconds, views, False

    report = options.get("autodelete_report", False)
    if type(report) is not bool:
        return "invalid", None, None, False
    if report and not allow_report:
        return "invalid", None, None, False
    return "mixed", seconds, views, bool(report)


class PublicationMixedAutodeleteService:
    """Shared destructive authority for canonical mixed time+views autodelete.

    Readiness remains trigger-specific: timer proves the durable due time and views proves
    the indexed threshold. Both triggers re-prove one immutable Publication occurrence
    and reserve DELETE authority through the same SQL PublicationAutodeleteActionLedger.
    The trigger source is deliberately absent from the authority fingerprint.

    Historical linked PostTask rows and any pre-existing views meta-ledger evidence are
    fail-closed here. Their established compatibility owners remain responsible until the
    later drain stages.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        delete_provider: TelegramDeleteProvider,
        view_source: TelegramMessageViewsSource | None = None,
        allow_report: bool = False,
        next_check_seconds: int = 60,
    ) -> None:
        self.session = session
        self.delete_provider = delete_provider
        self.view_source = view_source
        self.allow_report = bool(allow_report)
        self.next_check_seconds = max(15, min(int(next_check_seconds), 3600))

    def _candidate_from_row(
        self,
        *,
        publication: Publication,
        schedule: ScheduleEntry,
        item: ContentItem,
        channel: Channel,
        view_state: PublicationAutodeleteViewState | None,
    ) -> tuple[_Candidate | None, PublicationMixedAutodeleteResult | None]:
        publication_id = int(publication.id)
        ids = tuple(normalize_telegram_message_ids(publication.telegram_message_ids))
        meta = _mapping(publication.meta)
        if meta is None:
            return None, PublicationMixedAutodeleteResult(publication_id, "ineligible")

        intent, seconds, views, report_enabled = _mixed_intent(
            meta,
            allow_report=self.allow_report,
        )
        if intent == "not_mixed":
            return None, None
        if intent != "mixed" or seconds is None or views is None:
            return None, PublicationMixedAutodeleteResult(publication_id, "ineligible")

        if publication.legacy_post_task_id is not None:
            return None, PublicationMixedAutodeleteResult(
                publication_id,
                "ineligible",
                threshold=views,
                message_count=len(ids),
            )
        if AUTODELETE_VIEWS_ACTIONS_META_KEY in meta:
            # Old meta-ledger evidence is never reinterpreted as a clean SQL authority.
            return None, PublicationMixedAutodeleteResult(
                publication_id,
                "ambiguous",
                threshold=views,
                message_count=len(ids),
                ambiguous_count=1,
            )
        if not _safe_nonrepeat(schedule) or not ids:
            return None, PublicationMixedAutodeleteResult(
                publication_id,
                "ineligible",
                threshold=views,
                message_count=len(ids),
            )

        raw_runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
        if not isinstance(raw_runtime, Mapping):
            return None, PublicationMixedAutodeleteResult(
                publication_id,
                "ineligible",
                threshold=views,
                message_count=len(ids),
            )
        runtime = _mapping(raw_runtime)
        if runtime is None:
            return None, PublicationMixedAutodeleteResult(publication_id, "ineligible")
        deleted = runtime.get("deleted")
        if deleted is not None and type(deleted) is not bool:
            return None, PublicationMixedAutodeleteResult(publication_id, "ineligible")
        if deleted is True:
            return None, PublicationMixedAutodeleteResult(
                publication_id,
                "already_deleted",
                threshold=views,
                message_count=len(ids),
            )
        effective_seconds = _positive_int(runtime.get("effective_seconds"))
        due_at, due_token = _runtime_due_at(runtime)
        if effective_seconds != seconds or due_at is None or due_token is None:
            return None, PublicationMixedAutodeleteResult(
                publication_id,
                "ineligible",
                threshold=views,
                message_count=len(ids),
            )
        if view_state is None:
            return None, PublicationMixedAutodeleteResult(
                publication_id,
                "ineligible",
                threshold=views,
                message_count=len(ids),
            )
        if int(view_state.threshold) != views:
            return None, PublicationMixedAutodeleteResult(
                publication_id,
                "ineligible",
                threshold=views,
                message_count=len(ids),
            )

        runtime_options = _mapping(meta.get("runtime_options"))
        repeat_rule = _mapping(schedule.repeat_rule)
        if runtime_options is None or repeat_rule is None:
            return None, PublicationMixedAutodeleteResult(publication_id, "ineligible")
        result_link = normalize_telegram_result_link(publication.result_link)
        fingerprint = _fingerprint(
            {
                "version": 2,
                "publication_id": publication_id,
                "channel_id": int(publication.channel_id),
                "telegram_chat_id": int(channel.tg_chat_id),
                "content_item_id": int(item.id),
                "content_revision": int(publication.content_revision),
                "schedule_entry_id": int(schedule.id),
                "schedule_scheduled_at": _utc(schedule.scheduled_at).isoformat(),
                "schedule_timezone": schedule.timezone,
                "runtime_scheduled_at": due_token,
                "effective_seconds": effective_seconds,
                "runtime_options": runtime_options,
                "repeat_rule": repeat_rule,
                "telegram_message_ids": list(ids),
                "result_link": result_link,
                "report_enabled": report_enabled,
            }
        )
        if fingerprint is None:
            return None, PublicationMixedAutodeleteResult(publication_id, "ineligible")

        return (
            _Candidate(
                publication_id=publication_id,
                channel_id=int(publication.channel_id),
                tg_chat_id=int(channel.tg_chat_id),
                content_item_id=int(item.id),
                content_revision=int(publication.content_revision),
                schedule_entry_id=int(schedule.id),
                due_at=due_at,
                runtime_scheduled_at=due_token,
                effective_seconds=effective_seconds,
                threshold=views,
                telegram_message_ids=ids,
                report_enabled=report_enabled,
                result_link=result_link,
                authority_fingerprint=fingerprint,
                next_check_at=_utc(view_state.next_check_at),
            ),
            PublicationMixedAutodeleteResult(
                publication_id,
                "retry",
                threshold=views,
                message_count=len(ids),
            ),
        )

    async def _load_candidate(
        self,
        publication_id: int,
        *,
        lock: bool,
    ) -> tuple[_Candidate | None, PublicationMixedAutodeleteResult | None]:
        statement = (
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
            .outerjoin(
                PublicationAutodeleteViewState,
                PublicationAutodeleteViewState.publication_id == Publication.id,
            )
            .where(
                Publication.id == int(publication_id),
                Publication.status == "published",
                ScheduleEntry.status == "completed",
            )
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None, PublicationMixedAutodeleteResult(
                int(publication_id), "ineligible"
            )
        return self._candidate_from_row(
            publication=row[0],
            schedule=row[1],
            item=row[2],
            channel=row[3],
            view_state=row[4],
        )

    async def _candidate(
        self, publication_id: int
    ) -> tuple[_Candidate | None, PublicationMixedAutodeleteResult | None]:
        candidate, result = await self._load_candidate(publication_id, lock=False)
        await self.session.rollback()
        return candidate, result

    async def _reserve_message(
        self,
        candidate: _Candidate,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        message_id: int,
        now: datetime,
    ):
        current, early = await self._load_candidate(candidate.publication_id, lock=True)
        if current is None:
            await self.session.rollback()
            if early is not None and early.outcome == "already_deleted":
                return "already_deleted", None
            return "conflict", None
        if current != candidate:
            await self.session.rollback()
            return "conflict", None
        result = await PublicationAutodeleteActionLedger(self.session).reserve(
            handle,
            telegram_chat_id=candidate.tg_chat_id,
            telegram_message_id=int(message_id),
            authority_fingerprint=candidate.authority_fingerprint,
            now=now,
        )
        return result.outcome, result

    async def _mark_action(
        self,
        reservation: PublicationAutodeleteActionReservation,
        state: Literal["succeeded", "unavailable", "unknown"],
    ) -> bool:
        ledger = PublicationAutodeleteActionLedger(self.session)
        if state == "succeeded":
            return await ledger.mark_succeeded(reservation)
        if state == "unavailable":
            return await ledger.mark_unavailable(reservation)
        return await ledger.mark_unknown(reservation)

    async def _best_effort_mark_action(
        self,
        reservation: PublicationAutodeleteActionReservation,
        state: Literal["succeeded", "unavailable", "unknown"],
    ) -> bool:
        try:
            return await self._mark_action(reservation, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Publication mixed autodelete mark failed publication_id={} "
                "message_id={} state={} type={}",
                int(reservation.publication_id),
                int(reservation.telegram_message_id),
                state,
                type(exc).__name__,
            )
            return False

    async def _finalize(
        self,
        candidate: _Candidate,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        deleted_at: datetime,
    ) -> PublicationMixedAutodeleteResult:
        current, early = await self._load_candidate(candidate.publication_id, lock=True)
        if current is None:
            await self.session.rollback()
            if early is not None and early.outcome == "already_deleted":
                return early
            raise PublicationMixedAutodeleteSyncConflict()
        if current != candidate:
            await self.session.rollback()
            raise PublicationMixedAutodeleteSyncConflict()

        lease = (
            await self.session.execute(
                select(PublicationAutodeleteLease)
                .where(
                    PublicationAutodeleteLease.publication_id == candidate.publication_id,
                    PublicationAutodeleteLease.lease_token == str(handle.lease_token),
                    PublicationAutodeleteLease.expires_at > _utc(),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            await self.session.rollback()
            return PublicationMixedAutodeleteResult(
                candidate.publication_id,
                "retry",
                threshold=candidate.threshold,
                message_count=len(candidate.telegram_message_ids),
            )

        actions = (
            await self.session.execute(
                select(PublicationAutodeleteAction)
                .where(PublicationAutodeleteAction.publication_id == candidate.publication_id)
                .with_for_update()
            )
        ).scalars().all()
        expected_ids = set(candidate.telegram_message_ids)
        if (
            len(actions) != len(expected_ids)
            or {int(action.telegram_message_id) for action in actions} != expected_ids
            or any(
                int(action.telegram_chat_id) != candidate.tg_chat_id
                or str(action.authority_fingerprint) != candidate.authority_fingerprint
                or str(action.state) not in {"succeeded", "unavailable"}
                for action in actions
            )
        ):
            ambiguous = any(
                str(action.state) in {"reserved", "unknown"} for action in actions
            )
            await self.session.rollback()
            if ambiguous:
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "ambiguous",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                    ambiguous_count=1,
                )
            raise PublicationMixedAutodeleteSyncConflict()

        publication = await self.session.get(Publication, candidate.publication_id)
        if publication is None:
            await self.session.rollback()
            raise PublicationMixedAutodeleteSyncConflict()
        meta = _mapping(publication.meta)
        runtime = _mapping(meta.get(AUTODELETE_RUNTIME_META_KEY)) if meta is not None else None
        if meta is None or runtime is None:
            await self.session.rollback()
            raise PublicationMixedAutodeleteSyncConflict()
        runtime["deleted"] = True
        runtime["deleted_at"] = deleted_at.isoformat()
        new_meta = deepcopy(meta)
        new_meta[AUTODELETE_RUNTIME_META_KEY] = runtime
        publication.meta = new_meta
        state = await self.session.get(
            PublicationAutodeleteViewState, candidate.publication_id
        )
        if state is not None:
            await self.session.delete(state)
        await self.session.commit()
        return PublicationMixedAutodeleteResult(
            candidate.publication_id,
            "deleted",
            threshold=candidate.threshold,
            message_count=len(candidate.telegram_message_ids),
            deleted_count=sum(1 for action in actions if action.state == "succeeded"),
            unavailable_count=sum(1 for action in actions if action.state == "unavailable"),
        )

    async def _execute_delete(
        self,
        candidate: _Candidate,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        now: datetime,
    ) -> tuple[PublicationMixedAutodeleteResult, int]:
        deleted_count = 0
        unavailable_count = 0
        local_succeeded = 0
        for message_id in candidate.telegram_message_ids:
            outcome, reserve_result = await self._reserve_message(
                candidate,
                handle,
                message_id=int(message_id),
                now=_utc(),
            )
            if outcome == "already_deleted":
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "already_deleted",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                ), local_succeeded
            if outcome == "conflict":
                raise PublicationMixedAutodeleteSyncConflict()
            if outcome == "ineligible":
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "retry",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                ), local_succeeded
            if outcome == "ambiguous":
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "ambiguous",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                ), local_succeeded
            if reserve_result is None:
                raise PublicationMixedAutodeleteSyncConflict()
            if outcome == "already_terminal":
                state = str(reserve_result.existing_state)
                if state == "succeeded":
                    deleted_count += 1
                elif state == "unavailable":
                    unavailable_count += 1
                else:
                    raise PublicationMixedAutodeleteSyncConflict()
                continue

            reservation = reserve_result.reservation
            if reservation is None:
                raise PublicationMixedAutodeleteSyncConflict()
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
                        reservation, "unavailable"
                    )
                    if not marked:
                        return PublicationMixedAutodeleteResult(
                            candidate.publication_id,
                            "ambiguous",
                            threshold=candidate.threshold,
                            message_count=len(candidate.telegram_message_ids),
                            deleted_count=deleted_count,
                            unavailable_count=unavailable_count,
                            ambiguous_count=1,
                        ), local_succeeded
                    unavailable_count += 1
                    continue
                await self._best_effort_mark_action(reservation, "unknown")
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "ambiguous",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                ), local_succeeded

            marked = await self._best_effort_mark_action(reservation, "succeeded")
            if not marked:
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "ambiguous",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                ), local_succeeded
            deleted_count += 1
            local_succeeded += 1

        final = await self._finalize(candidate, handle, deleted_at=now)
        return final, local_succeeded

    async def _send_report_best_effort(
        self,
        candidate: _Candidate,
        *,
        trigger: Literal["timer", "views"],
    ) -> None:
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
            text = (
                "🗑️ Пост удалён по таймеру"
                if trigger == "timer"
                else "🗑️ Пост удалён по просмотрам"
            )
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
                "Publication mixed autodelete report failed publication_id={} type={}",
                candidate.publication_id,
                type(exc).__name__,
            )

    async def _defer_views(
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
                    == candidate.publication_id,
                    PublicationAutodeleteViewState.threshold == candidate.threshold,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state is None:
            await self.session.rollback()
            raise PublicationMixedAutodeleteSyncConflict()
        state.next_check_at = now + timedelta(seconds=self.next_check_seconds)
        if observed_views is not None:
            state.last_views = int(observed_views)
            state.last_checked_at = now
        await self.session.commit()

    async def timer_delete_if_due(
        self,
        publication_id: int,
        *,
        now: datetime | None = None,
        lease: PublicationAutodeleteLeaseHandle | None = None,
    ) -> PublicationMixedAutodeleteResult | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return PublicationMixedAutodeleteResult(0, "ineligible")
        current = _utc(now)
        candidate, early = await self._candidate(safe_publication_id)
        if candidate is None:
            return early
        if candidate.due_at > current:
            return PublicationMixedAutodeleteResult(
                candidate.publication_id,
                "not_due",
                threshold=candidate.threshold,
                message_count=len(candidate.telegram_message_ids),
            )

        handle = lease
        owns_lease = False
        release_after = True
        if handle is None:
            handle = await PublicationAutodeleteLeaseService(self.session).acquire(
                publication_id=candidate.publication_id,
                holder="publication-mixed-autodelete-timer",
                now=current,
            )
            if handle is None:
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "retry",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                )
            owns_lease = True
        try:
            result, local_succeeded = await self._execute_delete(
                candidate, handle, now=current
            )
            if result.outcome == "deleted" and local_succeeded > 0:
                await self._send_report_best_effort(candidate, trigger="timer")
            return result
        except asyncio.CancelledError:
            if owns_lease:
                release_after = False
            raise
        finally:
            if owns_lease and release_after:
                try:
                    await PublicationAutodeleteLeaseService(self.session).release(handle)
                except Exception as exc:
                    logger.warning(
                        "Publication mixed timer lease release failed publication_id={} type={}",
                        candidate.publication_id,
                        type(exc).__name__,
                    )

    async def views_evaluate_and_delete(
        self,
        publication_id: int,
        *,
        lease: PublicationAutodeleteLeaseHandle,
        now: datetime | None = None,
    ) -> PublicationMixedAutodeleteResult | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return PublicationMixedAutodeleteResult(0, "ineligible")
        current = _utc(now)
        candidate, early = await self._candidate(safe_publication_id)
        if candidate is None:
            return early
        if self.view_source is None or int(lease.publication_id) != candidate.publication_id:
            return PublicationMixedAutodeleteResult(
                candidate.publication_id,
                "ineligible",
                threshold=candidate.threshold,
                message_count=len(candidate.telegram_message_ids),
            )
        if candidate.next_check_at > current:
            return PublicationMixedAutodeleteResult(
                candidate.publication_id,
                "not_due",
                threshold=candidate.threshold,
                message_count=len(candidate.telegram_message_ids),
            )

        counts: list[int] = []
        for message_id in candidate.telegram_message_ids:
            try:
                raw_views = await self.view_source.get_message_views(
                    candidate.tg_chat_id, int(message_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._defer_views(candidate, now=current)
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "deferred",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                )
            views = _nonnegative_int(raw_views)
            if views is None:
                await self._defer_views(candidate, now=current)
                return PublicationMixedAutodeleteResult(
                    candidate.publication_id,
                    "deferred",
                    threshold=candidate.threshold,
                    message_count=len(candidate.telegram_message_ids),
                )
            counts.append(views)

        observed_views = min(counts)
        if observed_views < candidate.threshold:
            await self._defer_views(
                candidate,
                now=current,
                observed_views=observed_views,
            )
            return PublicationMixedAutodeleteResult(
                candidate.publication_id,
                "below_threshold",
                threshold=candidate.threshold,
                observed_views=observed_views,
                message_count=len(candidate.telegram_message_ids),
            )

        result, local_succeeded = await self._execute_delete(
            candidate, lease, now=current
        )
        result = PublicationMixedAutodeleteResult(
            publication_id=result.publication_id,
            outcome=result.outcome,
            threshold=candidate.threshold,
            observed_views=observed_views,
            message_count=result.message_count,
            deleted_count=result.deleted_count,
            unavailable_count=result.unavailable_count,
            ambiguous_count=result.ambiguous_count,
        )
        if result.outcome == "deleted" and local_succeeded > 0:
            await self._send_report_best_effort(candidate, trigger="views")
        return result
