from __future__ import annotations

import asyncio
import hashlib
import json
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
from app.domain.publication_autodelete import (
    PublicationAutodeleteAction,
    PublicationAutodeleteLease,
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
from app.services.publication_mixed_autodelete import (
    PublicationMixedAutodeleteService,
    PublicationMixedAutodeleteSyncConflict,
)
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
    """Canonical destructive authority changed across an autodelete boundary."""

    def __init__(self) -> None:
        super().__init__("canonical autodelete sync conflict")


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteResult:
    publication_id: int
    outcome: Literal[
        "deleted",
        "already_deleted",
        "not_due",
        "ineligible",
        "retry",
        "ambiguous",
    ]
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
    runtime_scheduled_at: str
    telegram_message_ids: tuple[int, ...]
    report_enabled: bool
    result_link: str | None
    authority_fingerprint: str


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


class PublicationAutodeleteService:
    """Delete due canonical-only publications through durable per-message claims.

    The publication lease serializes workers. Before each Telegram call, exact current
    canonical state and the exact live lease are proven under locks and a deterministic
    per-message action is committed. That committed reservation is the destructive
    authority linearization point. Telegram is then called outside any DB transaction
    using only the chat/message captured by the reservation.

    Existing ``reserved`` or ``unknown`` actions are permanent no-replay barriers.
    Terminal ``succeeded``/``unavailable`` actions let a multi-message batch resume after
    a clean crash between messages without repeating already-resolved deletes.
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

    def _candidate_from_row(
        self,
        *,
        publication: Publication,
        schedule: ScheduleEntry,
        item: ContentItem,
        channel: Channel,
        now: datetime,
    ) -> tuple[_Candidate | None, PublicationAutodeleteResult]:
        publication_id = int(publication.id)
        channel_id = int(publication.channel_id)
        tg_chat_id = int(channel.tg_chat_id)
        content_item_id = int(item.id)
        content_revision = int(publication.content_revision)
        schedule_entry_id = int(schedule.id)

        meta = _safe_mapping(publication.meta)
        raw_runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
        if raw_runtime is not None and not isinstance(raw_runtime, Mapping):
            return None, PublicationAutodeleteResult(publication_id, "ineligible")
        options_safe, report_enabled = _safe_time_only_options(
            meta,
            allow_report=self.allow_report,
        )
        if not options_safe:
            return None, PublicationAutodeleteResult(publication_id, "ineligible")

        runtime = _safe_mapping(raw_runtime)
        ids = tuple(normalize_telegram_message_ids(publication.telegram_message_ids))
        deleted_flag = runtime.get("deleted")
        if deleted_flag is not None and not isinstance(deleted_flag, bool):
            return None, PublicationAutodeleteResult(publication_id, "ineligible")
        if deleted_flag is True:
            return None, PublicationAutodeleteResult(
                publication_id,
                "already_deleted",
                message_count=len(ids),
            )
        if not _safe_nonrepeat(schedule):
            return None, PublicationAutodeleteResult(publication_id, "ineligible")

        due_at, due_token = _runtime_due_at(runtime)
        if due_at is None or due_token is None or not ids:
            return None, PublicationAutodeleteResult(publication_id, "ineligible")
        if due_at > now:
            return None, PublicationAutodeleteResult(
                publication_id,
                "not_due",
                message_count=len(ids),
            )

        raw_options = meta.get("runtime_options")
        if raw_options is not None and not isinstance(raw_options, Mapping):
            return None, PublicationAutodeleteResult(publication_id, "ineligible")
        runtime_options = _safe_mapping(raw_options)
        raw_repeat = schedule.repeat_rule
        if raw_repeat is not None and not isinstance(raw_repeat, Mapping):
            return None, PublicationAutodeleteResult(publication_id, "ineligible")
        repeat_rule = _safe_mapping(raw_repeat)
        result_link = normalize_telegram_result_link(publication.result_link)
        authority_fingerprint = _fingerprint(
            {
                "version": 1,
                "publication_id": publication_id,
                "channel_id": channel_id,
                "telegram_chat_id": tg_chat_id,
                "content_item_id": content_item_id,
                "content_revision": content_revision,
                "schedule_entry_id": schedule_entry_id,
                "schedule_scheduled_at": _utc(schedule.scheduled_at).isoformat(),
                "schedule_timezone": schedule.timezone,
                "runtime_scheduled_at": due_token,
                "runtime": runtime,
                "runtime_options": runtime_options,
                "repeat_rule": repeat_rule,
                "telegram_message_ids": list(ids),
                "result_link": result_link,
                "report_enabled": report_enabled,
            }
        )
        if authority_fingerprint is None:
            return None, PublicationAutodeleteResult(publication_id, "ineligible")

        return (
            _Candidate(
                publication_id=publication_id,
                channel_id=channel_id,
                tg_chat_id=tg_chat_id,
                content_item_id=content_item_id,
                content_revision=content_revision,
                schedule_entry_id=schedule_entry_id,
                runtime_scheduled_at=due_token,
                telegram_message_ids=ids,
                report_enabled=report_enabled,
                result_link=result_link,
                authority_fingerprint=authority_fingerprint,
            ),
            PublicationAutodeleteResult(
                publication_id,
                "retry",
                message_count=len(ids),
            ),
        )

    async def _load_candidate(
        self,
        publication_id: int,
        *,
        now: datetime,
        lock: bool,
    ) -> tuple[_Candidate | None, PublicationAutodeleteResult]:
        query = (
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
                Publication.status == "published",
                ScheduleEntry.status == "completed",
            )
        )
        if lock:
            query = query.with_for_update()
        row = (await self.session.execute(query)).one_or_none()
        if row is None:
            return None, PublicationAutodeleteResult(
                publication_id=int(publication_id),
                outcome="ineligible",
            )
        publication, schedule, item, channel = row
        return self._candidate_from_row(
            publication=publication,
            schedule=schedule,
            item=item,
            channel=channel,
            now=now,
        )

    async def _candidate(
        self,
        publication_id: int,
        *,
        now: datetime,
    ) -> tuple[_Candidate | None, PublicationAutodeleteResult]:
        candidate, result = await self._load_candidate(
            publication_id,
            now=now,
            lock=False,
        )
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
        current, early = await self._load_candidate(
            candidate.publication_id,
            now=now,
            lock=True,
        )
        if current is None:
            await self.session.rollback()
            if early.outcome == "already_deleted":
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
            # This service owns one operation-scoped AsyncSession. Do not shield a mark
            # on that same session: an outer cancellation could close the session while
            # the shielded coroutine continues. A direct cleanup await gets one chance
            # to persist terminal evidence; if it is interrupted, the already-committed
            # `reserved` row remains the permanent no-replay barrier.
            return await self._mark_action(reservation, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Publication autodelete action mark failed publication_id={} "
                "message_id={} state={} type={}",
                int(reservation.publication_id),
                int(reservation.telegram_message_id),
                state,
                type(exc).__name__,
            )
            return False

    async def _mark_deleted(
        self,
        candidate: _Candidate,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        deleted_at: datetime,
        authority_now: datetime,
    ) -> PublicationAutodeleteResult:
        current, early = await self._load_candidate(
            candidate.publication_id,
            now=authority_now,
            lock=True,
        )
        if current is None:
            await self.session.rollback()
            if early.outcome == "already_deleted":
                return PublicationAutodeleteResult(
                    publication_id=candidate.publication_id,
                    outcome="already_deleted",
                    message_count=len(candidate.telegram_message_ids),
                )
            raise PublicationAutodeleteSyncConflict()
        if current != candidate:
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()

        lease = (
            await self.session.execute(
                select(PublicationAutodeleteLease)
                .where(
                    PublicationAutodeleteLease.publication_id
                    == candidate.publication_id,
                    PublicationAutodeleteLease.lease_token
                    == str(handle.lease_token),
                    PublicationAutodeleteLease.expires_at > authority_now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            await self.session.rollback()
            return PublicationAutodeleteResult(
                publication_id=candidate.publication_id,
                outcome="retry",
                message_count=len(candidate.telegram_message_ids),
            )

        actions = (
            await self.session.execute(
                select(PublicationAutodeleteAction)
                .where(
                    PublicationAutodeleteAction.publication_id
                    == candidate.publication_id
                )
                .with_for_update()
            )
        ).scalars().all()
        expected_ids = set(candidate.telegram_message_ids)
        if (
            len(actions) != len(expected_ids)
            or {int(action.telegram_message_id) for action in actions} != expected_ids
            or any(
                int(action.telegram_chat_id) != candidate.tg_chat_id
                or str(action.authority_fingerprint)
                != candidate.authority_fingerprint
                or str(action.state) not in {"succeeded", "unavailable"}
                for action in actions
            )
        ):
            await self.session.rollback()
            if any(str(action.state) in {"reserved", "unknown"} for action in actions):
                return PublicationAutodeleteResult(
                    publication_id=candidate.publication_id,
                    outcome="ambiguous",
                    message_count=len(candidate.telegram_message_ids),
                    ambiguous_count=1,
                )
            raise PublicationAutodeleteSyncConflict()

        publication = await self.session.get(Publication, candidate.publication_id)
        if publication is None:
            await self.session.rollback()
            raise PublicationAutodeleteSyncConflict()
        meta = _safe_mapping(publication.meta)
        runtime = _safe_mapping(meta.get(AUTODELETE_RUNTIME_META_KEY))
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
            deleted_count=sum(1 for action in actions if action.state == "succeeded"),
            unavailable_count=sum(
                1 for action in actions if action.state == "unavailable"
            ),
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

    async def _delete_with_lease(
        self,
        publication_id: int,
        *,
        handle: PublicationAutodeleteLeaseHandle,
        now: datetime,
        fixed_authority_now: bool,
    ) -> PublicationAutodeleteResult:
        candidate, early = await self._candidate(publication_id, now=now)
        if candidate is None:
            return early
        if int(handle.publication_id) != candidate.publication_id:
            return PublicationAutodeleteResult(
                publication_id=candidate.publication_id,
                outcome="ineligible",
                message_count=len(candidate.telegram_message_ids),
            )

        deleted_count = 0
        unavailable_count = 0
        for message_id in candidate.telegram_message_ids:
            proof_now = now if fixed_authority_now else _utc()
            outcome, reserve_result = await self._reserve_message(
                candidate,
                handle,
                message_id=int(message_id),
                now=proof_now,
            )
            if outcome == "already_deleted":
                return PublicationAutodeleteResult(
                    publication_id=candidate.publication_id,
                    outcome="already_deleted",
                    message_count=len(candidate.telegram_message_ids),
                )
            if outcome == "conflict":
                raise PublicationAutodeleteSyncConflict()
            if outcome == "ineligible":
                return PublicationAutodeleteResult(
                    publication_id=candidate.publication_id,
                    outcome="retry",
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                )
            if outcome == "ambiguous":
                return PublicationAutodeleteResult(
                    publication_id=candidate.publication_id,
                    outcome="ambiguous",
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                )
            if outcome == "already_terminal":
                state = str(reserve_result.existing_state)
                if state == "succeeded":
                    deleted_count += 1
                elif state == "unavailable":
                    unavailable_count += 1
                else:
                    raise PublicationAutodeleteSyncConflict()
                continue

            reservation = reserve_result.reservation
            if reservation is None:
                raise PublicationAutodeleteSyncConflict()
            try:
                await self.provider.delete_message(
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
                        return PublicationAutodeleteResult(
                            candidate.publication_id,
                            "ambiguous",
                            message_count=len(candidate.telegram_message_ids),
                            deleted_count=deleted_count,
                            unavailable_count=unavailable_count,
                            ambiguous_count=1,
                        )
                    unavailable_count += 1
                    continue
                await self._best_effort_mark_action(reservation, "unknown")
                return PublicationAutodeleteResult(
                    candidate.publication_id,
                    "ambiguous",
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                )

            marked = await self._best_effort_mark_action(reservation, "succeeded")
            if not marked:
                return PublicationAutodeleteResult(
                    candidate.publication_id,
                    "ambiguous",
                    message_count=len(candidate.telegram_message_ids),
                    deleted_count=deleted_count,
                    unavailable_count=unavailable_count,
                    ambiguous_count=1,
                )
            deleted_count += 1

        final_now = now if fixed_authority_now else _utc()
        result = await self._mark_deleted(
            candidate,
            handle,
            deleted_at=now,
            authority_now=final_now,
        )
        if result.outcome == "deleted" and result.deleted_count > 0:
            await self._send_report_best_effort(candidate)
        return result

    async def delete_if_due(
        self,
        publication_id: int,
        *,
        now: datetime | None = None,
        lease: PublicationAutodeleteLeaseHandle | None = None,
    ) -> PublicationAutodeleteResult:
        try:
            publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return PublicationAutodeleteResult(0, "ineligible")
        if publication_id <= 0:
            return PublicationAutodeleteResult(publication_id, "ineligible")

        try:
            mixed = await PublicationMixedAutodeleteService(
                self.session,
                delete_provider=self.provider,
                allow_report=self.allow_report,
            ).timer_delete_if_due(
                publication_id,
                now=now,
                lease=lease,
            )
        except PublicationMixedAutodeleteSyncConflict as exc:
            raise PublicationAutodeleteSyncConflict() from exc
        if mixed is not None:
            outcome = (
                mixed.outcome
                if mixed.outcome
                in {"deleted", "already_deleted", "not_due", "ineligible", "retry", "ambiguous"}
                else "retry"
            )
            return PublicationAutodeleteResult(
                publication_id=mixed.publication_id,
                outcome=outcome,
                message_count=mixed.message_count,
                deleted_count=mixed.deleted_count,
                unavailable_count=mixed.unavailable_count,
                ambiguous_count=mixed.ambiguous_count,
            )

        fixed_authority_now = now is not None
        current = _utc(now)
        handle = lease
        owns_lease = False
        release_after = True
        if handle is None:
            preflight, early = await self._candidate(publication_id, now=current)
            if preflight is None:
                return early
            handle = await PublicationAutodeleteLeaseService(self.session).acquire(
                publication_id=publication_id,
                holder="publication-autodelete-service",
                now=current,
            )
            if handle is None:
                return PublicationAutodeleteResult(
                    publication_id,
                    "retry",
                    message_count=len(preflight.telegram_message_ids),
                )
            owns_lease = True

        try:
            return await self._delete_with_lease(
                publication_id,
                handle=handle,
                now=current,
                fixed_authority_now=fixed_authority_now,
            )
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
                        "Publication autodelete self-owned lease release failed "
                        "publication_id={} type={}",
                        publication_id,
                        type(exc).__name__,
                    )
