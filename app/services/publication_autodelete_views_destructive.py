from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle
from app.services.publication_autodelete_views import (
    PublicationAutodeleteViewsService,
    PublicationAutodeleteViewsSyncConflict,
    _Candidate,
    _is_unavailable_delete_error,
    _mapping,
    _nonnegative_view_count,
    _utc,
)
from app.services.publication_autodelete_views_action_ledger import (
    PublicationAutodeleteViewsActionLedger,
    PublicationAutodeleteViewsActionReservation,
    views_action_authority_fingerprint,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import normalize_telegram_result_link


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewsDestructiveResult:
    publication_id: int
    outcome: Literal[
        "deleted",
        "already_deleted",
        "below_threshold",
        "deferred",
        "not_due",
        "ineligible",
        "ambiguous",
    ]
    threshold: int | None = None
    observed_views: int | None = None
    message_count: int = 0
    deleted_count: int = 0
    unavailable_count: int = 0
    ambiguous_count: int = 0


def _result(
    candidate: _Candidate,
    outcome: Literal[
        "deleted",
        "already_deleted",
        "below_threshold",
        "deferred",
        "not_due",
        "ineligible",
        "ambiguous",
    ],
    *,
    observed_views: int | None = None,
    deleted_count: int = 0,
    unavailable_count: int = 0,
    ambiguous_count: int = 0,
) -> PublicationAutodeleteViewsDestructiveResult:
    return PublicationAutodeleteViewsDestructiveResult(
        publication_id=candidate.publication_id,
        outcome=outcome,
        threshold=candidate.threshold,
        observed_views=observed_views,
        message_count=len(candidate.telegram_message_ids),
        deleted_count=deleted_count,
        unavailable_count=unavailable_count,
        ambiguous_count=ambiguous_count,
    )


class PublicationAutodeleteViewsDestructiveService(PublicationAutodeleteViewsService):
    """Reserve-before-DELETE boundary for the existing views evaluator.

    Candidate admission, observations and repeat lifecycle proof are inherited from the
    #287 service. The production worker uses this subclass and must pass the exact lease
    it acquired. Before every Telegram DELETE, the service re-proves current authority,
    locks Schedule/Channel identity and commits one per-message reservation. Provider
    calls happen only after that commit and use only immutable identity from reservation.

    The ledger API intentionally mirrors the independent #281 destructive-action state
    machine while its current storage remains occurrence-local Publication.meta. This is
    the future convergence seam once the independent shared schema is mergeable.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        view_source,
        delete_provider,
        next_check_seconds: int = 60,
        allow_report: bool = False,
        allow_repeat_views: bool = False,
        lease_handle: PublicationAutodeleteLeaseHandle | None = None,
    ) -> None:
        super().__init__(
            session,
            view_source=view_source,
            delete_provider=delete_provider,
            next_check_seconds=next_check_seconds,
            allow_report=allow_report,
            allow_repeat_views=allow_repeat_views,
        )
        self.lease_handle = lease_handle

    async def _locked_authority(
        self,
        candidate: _Candidate,
    ) -> tuple[Publication, PublicationAutodeleteViewState, str] | Literal[
        "already_deleted"
    ]:
        current = await super()._load_current(candidate, require_state=True)
        if current == "already_deleted":
            return "already_deleted"
        publication, state = current
        if state is None:
            raise PublicationAutodeleteViewsSyncConflict()

        schedule = (
            await self.session.execute(
                select(ScheduleEntry)
                .where(ScheduleEntry.id == candidate.schedule_entry_id)
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
            or channel is None
            or str(schedule.status) != "completed"
            or int(schedule.channel_id) != candidate.channel_id
            or int(schedule.content_item_id) != candidate.content_item_id
            or int(schedule.content_revision) != candidate.content_revision
            or int(channel.tg_chat_id) != candidate.tg_chat_id
        ):
            raise PublicationAutodeleteViewsSyncConflict()

        meta = _mapping(publication.meta)
        if meta is None:
            raise PublicationAutodeleteViewsSyncConflict()
        raw_options = meta.get("runtime_options")
        raw_runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
        raw_repeat = schedule.repeat_rule
        raw_schedule_meta = schedule.meta
        if (
            (raw_options is not None and not isinstance(raw_options, Mapping))
            or (raw_runtime is not None and not isinstance(raw_runtime, Mapping))
            or (raw_repeat is not None and not isinstance(raw_repeat, Mapping))
            or (raw_schedule_meta is not None and not isinstance(raw_schedule_meta, Mapping))
        ):
            raise PublicationAutodeleteViewsSyncConflict()
        options = dict(raw_options or {})
        runtime = dict(raw_runtime or {})
        repeat_rule = dict(raw_repeat or {})
        schedule_meta = dict(raw_schedule_meta or {})
        raw_schedule_options = schedule_meta.get("runtime_options")
        if raw_schedule_options is not None and not isinstance(
            raw_schedule_options, Mapping
        ):
            raise PublicationAutodeleteViewsSyncConflict()
        schedule_options = dict(raw_schedule_options or {})

        scheduled_at = schedule.scheduled_at
        if not isinstance(scheduled_at, datetime):
            raise PublicationAutodeleteViewsSyncConflict()
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        else:
            scheduled_at = scheduled_at.astimezone(timezone.utc)

        fingerprint = views_action_authority_fingerprint(
            {
                "version": 1,
                "publication_id": candidate.publication_id,
                "channel_id": candidate.channel_id,
                "telegram_chat_id": candidate.tg_chat_id,
                "content_item_id": candidate.content_item_id,
                "content_revision": candidate.content_revision,
                "schedule_entry_id": candidate.schedule_entry_id,
                "schedule_scheduled_at": scheduled_at.isoformat(),
                "schedule_timezone": schedule.timezone,
                "repeat_rule": repeat_rule,
                "publication_repeat_group_id": meta.get("repeat_group_id"),
                "schedule_repeat_group_id": schedule_meta.get("repeat_group_id"),
                "runtime_options": options,
                "schedule_runtime_options": schedule_options,
                "autodelete_runtime": runtime,
                "legacy_post_task_id": candidate.legacy_post_task_id,
                "publication_attempt_count": int(publication.attempt_count),
                "telegram_message_ids": list(candidate.telegram_message_ids),
                "view_threshold": candidate.threshold,
                "report_enabled": candidate.report_enabled,
                "result_link": normalize_telegram_result_link(publication.result_link),
            }
        )
        if fingerprint is None:
            raise PublicationAutodeleteViewsSyncConflict()
        return publication, state, fingerprint

    async def _snapshot_fingerprint(
        self,
        candidate: _Candidate,
    ) -> str | Literal["already_deleted"]:
        locked = await self._locked_authority(candidate)
        if locked == "already_deleted":
            await self.session.rollback()
            return "already_deleted"
        _, _, fingerprint = locked
        await self.session.rollback()
        return fingerprint

    async def _barrier(
        self,
        candidate: _Candidate,
        *,
        fingerprint: str,
    ) -> Literal["clear", "ambiguous", "conflict", "already_deleted"]:
        locked = await self._locked_authority(candidate)
        if locked == "already_deleted":
            await self.session.rollback()
            return "already_deleted"
        publication, _, current = locked
        if current != fingerprint:
            await self.session.rollback()
            return "conflict"
        snapshot = PublicationAutodeleteViewsActionLedger(self.session).snapshot_locked(
            publication,
            telegram_chat_id=candidate.tg_chat_id,
            expected_message_ids=candidate.telegram_message_ids,
            authority_fingerprint=fingerprint,
        )
        await self.session.rollback()
        if snapshot.outcome == "ambiguous":
            return "ambiguous"
        if snapshot.outcome == "conflict":
            return "conflict"
        return "clear"

    async def _reserve(
        self,
        candidate: _Candidate,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        message_id: int,
        fingerprint: str,
    ):
        locked = await self._locked_authority(candidate)
        if locked == "already_deleted":
            await self.session.rollback()
            return "already_deleted", None
        publication, _, current = locked
        if current != fingerprint:
            await self.session.rollback()
            return "conflict", None
        result = await PublicationAutodeleteViewsActionLedger(
            self.session
        ).reserve_locked(
            publication,
            handle,
            telegram_chat_id=candidate.tg_chat_id,
            telegram_message_id=message_id,
            expected_message_ids=candidate.telegram_message_ids,
            authority_fingerprint=fingerprint,
            now=_utc(),
        )
        return result.outcome, result

    async def _finish_action(
        self,
        reservation: PublicationAutodeleteViewsActionReservation,
        state: Literal["succeeded", "unavailable", "unknown"],
    ) -> bool:
        ledger = PublicationAutodeleteViewsActionLedger(self.session)
        try:
            if state == "succeeded":
                return await ledger.mark_succeeded(reservation)
            if state == "unavailable":
                return await ledger.mark_unavailable(reservation)
            return await ledger.mark_unknown(reservation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Publication views action finalize failed publication_id={} "
                "message_id={} state={} type={}",
                reservation.publication_id,
                reservation.telegram_message_id,
                state,
                type(exc).__name__,
            )
            return False

    async def _mark_deleted_from_actions(
        self,
        candidate: _Candidate,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        fingerprint: str,
        observed_views: int,
        deleted_at: datetime,
    ) -> PublicationAutodeleteViewsDestructiveResult:
        locked = await self._locked_authority(candidate)
        if locked == "already_deleted":
            await self.session.rollback()
            await self._cleanup_terminal_state(candidate.publication_id)
            return _result(candidate, "already_deleted", observed_views=observed_views)
        publication, state, current = locked
        if current != fingerprint:
            await self.session.rollback()
            raise PublicationAutodeleteViewsSyncConflict()

        ledger = PublicationAutodeleteViewsActionLedger(self.session)
        if not await ledger.has_live_lease_locked(
            handle,
            publication_id=candidate.publication_id,
            now=_utc(),
        ):
            await self.session.rollback()
            return _result(candidate, "ambiguous", observed_views=observed_views, ambiguous_count=1)
        snapshot = ledger.snapshot_locked(
            publication,
            telegram_chat_id=candidate.tg_chat_id,
            expected_message_ids=candidate.telegram_message_ids,
            authority_fingerprint=fingerprint,
        )
        if snapshot.outcome == "ambiguous":
            await self.session.rollback()
            return _result(candidate, "ambiguous", observed_views=observed_views, ambiguous_count=1)
        if snapshot.outcome != "terminal":
            await self.session.rollback()
            raise PublicationAutodeleteViewsSyncConflict()

        meta = _mapping(publication.meta)
        if meta is None:
            await self.session.rollback()
            raise PublicationAutodeleteViewsSyncConflict()
        new_meta = deepcopy(meta)
        new_meta[AUTODELETE_RUNTIME_META_KEY] = {
            "mode": "views",
            "view_threshold": candidate.threshold,
            "observed_views": observed_views,
            "deleted": True,
            "deleted_at": deleted_at.isoformat(),
        }
        publication.meta = new_meta
        await self.session.delete(state)
        await self.session.commit()
        return _result(
            candidate,
            "deleted",
            observed_views=observed_views,
            deleted_count=snapshot.succeeded_count,
            unavailable_count=snapshot.unavailable_count,
        )

    async def _delete_batch(
        self,
        candidate: _Candidate,
        *,
        handle: PublicationAutodeleteLeaseHandle,
        fingerprint: str,
        observed_views: int,
        deleted_at: datetime,
    ) -> PublicationAutodeleteViewsDestructiveResult:
        deleted = 0
        unavailable = 0
        for message_id in candidate.telegram_message_ids:
            outcome, reserved = await self._reserve(
                candidate,
                handle,
                message_id=int(message_id),
                fingerprint=fingerprint,
            )
            if outcome == "already_deleted":
                return _result(candidate, "already_deleted", observed_views=observed_views)
            if outcome == "conflict":
                raise PublicationAutodeleteViewsSyncConflict()
            if outcome in {"ineligible", "ambiguous"}:
                return _result(
                    candidate,
                    "ambiguous",
                    observed_views=observed_views,
                    deleted_count=deleted,
                    unavailable_count=unavailable,
                    ambiguous_count=1,
                )
            if outcome == "already_terminal":
                state = str(reserved.existing_state)
                if state == "succeeded":
                    deleted += 1
                elif state == "unavailable":
                    unavailable += 1
                else:
                    raise PublicationAutodeleteViewsSyncConflict()
                continue

            reservation = reserved.reservation
            if reservation is None:
                raise PublicationAutodeleteViewsSyncConflict()
            try:
                await self.delete_provider.delete_message(
                    chat_id=reservation.telegram_chat_id,
                    message_id=reservation.telegram_message_id,
                )
            except asyncio.CancelledError:
                try:
                    await self._finish_action(reservation, "unknown")
                finally:
                    raise
            except Exception as exc:
                if _is_unavailable_delete_error(exc):
                    if not await self._finish_action(reservation, "unavailable"):
                        return _result(candidate, "ambiguous", observed_views=observed_views, deleted_count=deleted, unavailable_count=unavailable, ambiguous_count=1)
                    unavailable += 1
                    continue
                await self._finish_action(reservation, "unknown")
                return _result(candidate, "ambiguous", observed_views=observed_views, deleted_count=deleted, unavailable_count=unavailable, ambiguous_count=1)

            if not await self._finish_action(reservation, "succeeded"):
                return _result(candidate, "ambiguous", observed_views=observed_views, deleted_count=deleted, unavailable_count=unavailable, ambiguous_count=1)
            deleted += 1

        result = await self._mark_deleted_from_actions(
            candidate,
            handle,
            fingerprint=fingerprint,
            observed_views=observed_views,
            deleted_at=deleted_at,
        )
        if result.outcome == "deleted" and result.deleted_count > 0:
            await self._send_report_best_effort(candidate)
        return result

    async def evaluate_and_delete(
        self,
        publication_id: int,
        *,
        now: datetime | None = None,
    ) -> PublicationAutodeleteViewsDestructiveResult:
        try:
            safe_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return PublicationAutodeleteViewsDestructiveResult(0, "ineligible")
        current = _utc(now)
        candidate, early = await self._candidate(safe_id, now=current)
        if candidate is None:
            allowed = {"already_deleted", "below_threshold", "deferred", "not_due", "ineligible"}
            outcome = early.outcome if early.outcome in allowed else "ineligible"
            return PublicationAutodeleteViewsDestructiveResult(
                publication_id=early.publication_id,
                outcome=outcome,
                threshold=early.threshold,
                observed_views=early.observed_views,
                message_count=early.message_count,
                deleted_count=early.deleted_count,
                unavailable_count=early.unavailable_count,
            )

        fingerprint = await self._snapshot_fingerprint(candidate)
        if fingerprint == "already_deleted":
            return _result(candidate, "already_deleted")
        barrier = await self._barrier(candidate, fingerprint=fingerprint)
        if barrier == "already_deleted":
            return _result(candidate, "already_deleted")
        if barrier == "ambiguous":
            return _result(candidate, "ambiguous", ambiguous_count=1)
        if barrier == "conflict":
            raise PublicationAutodeleteViewsSyncConflict()

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
                return _result(candidate, "deferred")
            views = _nonnegative_view_count(raw_views)
            if views is None:
                await self._defer(candidate, now=current)
                return _result(candidate, "deferred")
            counts.append(views)

        observed_views = min(counts)
        if observed_views < candidate.threshold:
            await self._defer(candidate, now=current, observed_views=observed_views)
            return _result(candidate, "below_threshold", observed_views=observed_views)

        predelete = await self._snapshot_fingerprint(candidate)
        if predelete == "already_deleted":
            return _result(candidate, "already_deleted", observed_views=observed_views)
        if predelete != fingerprint:
            raise PublicationAutodeleteViewsSyncConflict()

        handle = self.lease_handle
        if handle is None or int(handle.publication_id) != candidate.publication_id:
            return _result(candidate, "ineligible", observed_views=observed_views)
        return await self._delete_batch(
            candidate,
            handle=handle,
            fingerprint=fingerprint,
            observed_views=observed_views,
            deleted_at=current,
        )
