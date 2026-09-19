from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_autodelete import PublicationAutodeleteLease
from app.domain.publishing.models import Publication
from app.services.canonical_runtime_safety import has_no_replay_barrier
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle


AUTODELETE_VIEWS_ACTIONS_META_KEY = "autodelete_views_actions"
_VIEWS_ACTIONS_VERSION = 1
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATES = frozenset({"succeeded", "unavailable"})
_AMBIGUOUS_STATES = frozenset({"reserved", "unknown"})
_ALL_STATES = _TERMINAL_STATES | _AMBIGUOUS_STATES


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewsActionReservation:
    publication_id: int
    telegram_chat_id: int
    telegram_message_id: int
    authority_fingerprint: str
    reservation_token: str
    autodelete_lease_token: str
    autodelete_lease_holder: str


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewsActionReserveResult:
    outcome: Literal[
        "reserved",
        "already_terminal",
        "ambiguous",
        "conflict",
        "ineligible",
    ]
    reservation: PublicationAutodeleteViewsActionReservation | None = None
    existing_state: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewsActionInspection:
    outcome: Literal["clean", "ambiguous", "conflict"]
    observed_views: int | None = None
    succeeded_count: int = 0
    unavailable_count: int = 0
    action_count: int = 0


def inspect_publication_autodelete_views_actions(
    meta: Mapping[str, Any],
    *,
    authority_fingerprint: str,
    telegram_chat_id: int,
    telegram_message_ids: tuple[int, ...],
    threshold: int,
) -> PublicationAutodeleteViewsActionInspection:
    raw = meta.get(AUTODELETE_VIEWS_ACTIONS_META_KEY)
    if raw is None:
        return PublicationAutodeleteViewsActionInspection(outcome="clean")
    root = _mapping(raw)
    if root is None:
        return PublicationAutodeleteViewsActionInspection(outcome="conflict")
    if (
        root.get("version") != _VIEWS_ACTIONS_VERSION
        or str(root.get("authority_fingerprint", "")) != authority_fingerprint
        or root.get("telegram_chat_id") != int(telegram_chat_id)
        or root.get("telegram_message_ids") != list(telegram_message_ids)
        or root.get("threshold") != int(threshold)
    ):
        return PublicationAutodeleteViewsActionInspection(outcome="conflict")

    observed_views = root.get("observed_views")
    if (
        isinstance(observed_views, bool)
        or not isinstance(observed_views, int)
        or int(observed_views) < int(threshold)
    ):
        return PublicationAutodeleteViewsActionInspection(outcome="conflict")

    actions = _mapping(root.get("actions"))
    if actions is None:
        return PublicationAutodeleteViewsActionInspection(outcome="conflict")

    expected_ids = set(int(message_id) for message_id in telegram_message_ids)
    succeeded = 0
    unavailable = 0
    ambiguous = False
    seen: set[int] = set()

    for raw_key, raw_action in actions.items():
        action = _mapping(raw_action)
        if action is None:
            return PublicationAutodeleteViewsActionInspection(outcome="conflict")
        try:
            message_id = int(raw_key)
        except (TypeError, ValueError, OverflowError):
            return PublicationAutodeleteViewsActionInspection(outcome="conflict")
        if message_id not in expected_ids or message_id in seen:
            return PublicationAutodeleteViewsActionInspection(outcome="conflict")
        seen.add(message_id)

        state = str(action.get("state", ""))
        token = str(action.get("reservation_token", ""))
        lease_token = str(action.get("autodelete_lease_token", ""))
        lease_holder = str(action.get("autodelete_lease_holder", ""))
        if (
            action.get("telegram_message_id") != message_id
            or action.get("telegram_chat_id") != int(telegram_chat_id)
            or str(action.get("authority_fingerprint", "")) != authority_fingerprint
            or state not in _ALL_STATES
            or not token
            or not lease_token
            or not lease_holder
        ):
            return PublicationAutodeleteViewsActionInspection(outcome="conflict")
        if state == "succeeded":
            succeeded += 1
        elif state == "unavailable":
            unavailable += 1
        else:
            ambiguous = True

    return PublicationAutodeleteViewsActionInspection(
        outcome="ambiguous" if ambiguous else "clean",
        observed_views=int(observed_views),
        succeeded_count=succeeded,
        unavailable_count=unavailable,
        action_count=len(seen),
    )


class PublicationAutodeleteViewsActionLedger:
    """Schema-free one-way destructive authority ledger for views autodelete.

    The Publication row is the occurrence-local durable serialization point. A Telegram
    DELETE is authorized only by a newly committed ``reserved`` action under exact
    current views authority and an exact live autodelete lease. Existing
    ``reserved``/``unknown`` actions permanently block automatic destructive replay.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _valid_identity(
        *,
        publication_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        authority_fingerprint: str,
        telegram_message_ids: tuple[int, ...],
        threshold: int,
        observed_views: int,
    ) -> tuple[int, int, int, str, tuple[int, ...], int, int] | None:
        try:
            publication_id = int(publication_id)
            chat_id = int(telegram_chat_id)
            message_id = int(telegram_message_id)
            expected_ids = tuple(int(item) for item in telegram_message_ids)
            threshold = int(threshold)
            observed_views = int(observed_views)
        except (TypeError, ValueError, OverflowError):
            return None
        fingerprint = str(authority_fingerprint).strip().lower()
        if (
            publication_id <= 0
            or chat_id == 0
            or message_id <= 0
            or not expected_ids
            or message_id not in set(expected_ids)
            or len(set(expected_ids)) != len(expected_ids)
            or threshold <= 0
            or observed_views < threshold
            or not _FINGERPRINT_RE.fullmatch(fingerprint)
        ):
            return None
        return (
            publication_id,
            chat_id,
            message_id,
            fingerprint,
            expected_ids,
            threshold,
            observed_views,
        )

    async def _live_lease(
        self,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        at: datetime,
    ) -> PublicationAutodeleteLease | None:
        try:
            publication_id = int(handle.publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        token = str(handle.lease_token)
        holder = str(handle.holder)
        if publication_id <= 0 or not token or not holder:
            return None
        return (
            await self.session.execute(
                select(PublicationAutodeleteLease)
                .where(
                    PublicationAutodeleteLease.publication_id == publication_id,
                    PublicationAutodeleteLease.lease_token == token,
                    PublicationAutodeleteLease.holder == holder,
                    PublicationAutodeleteLease.expires_at > at,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def reserve(
        self,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
        authority_fingerprint: str,
        telegram_message_ids: tuple[int, ...],
        threshold: int,
        observed_views: int,
        now: datetime | None = None,
    ) -> PublicationAutodeleteViewsActionReserveResult:
        identity = self._valid_identity(
            publication_id=handle.publication_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            authority_fingerprint=authority_fingerprint,
            telegram_message_ids=telegram_message_ids,
            threshold=threshold,
            observed_views=observed_views,
        )
        if identity is None:
            return PublicationAutodeleteViewsActionReserveResult(outcome="ineligible")
        (
            publication_id,
            chat_id,
            message_id,
            fingerprint,
            expected_ids,
            safe_threshold,
            safe_observed_views,
        ) = identity
        current = _utc(now)

        try:
            lease = await self._live_lease(handle, at=current)
            if lease is None:
                await self.session.rollback()
                return PublicationAutodeleteViewsActionReserveResult(outcome="ineligible")
            if await has_no_replay_barrier(
                self.session,
                publication_id=publication_id,
            ):
                await self.session.rollback()
                return PublicationAutodeleteViewsActionReserveResult(outcome="ambiguous")

            publication = (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.id == publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if publication is None:
                await self.session.rollback()
                return PublicationAutodeleteViewsActionReserveResult(outcome="conflict")

            meta = _mapping(publication.meta)
            if meta is None:
                await self.session.rollback()
                return PublicationAutodeleteViewsActionReserveResult(outcome="conflict")

            inspection = inspect_publication_autodelete_views_actions(
                meta,
                authority_fingerprint=fingerprint,
                telegram_chat_id=chat_id,
                telegram_message_ids=expected_ids,
                threshold=safe_threshold,
            )
            raw_root = meta.get(AUTODELETE_VIEWS_ACTIONS_META_KEY)
            if raw_root is not None:
                if inspection.outcome == "conflict":
                    await self.session.rollback()
                    return PublicationAutodeleteViewsActionReserveResult(outcome="conflict")
                if inspection.observed_views != safe_observed_views:
                    await self.session.rollback()
                    return PublicationAutodeleteViewsActionReserveResult(outcome="conflict")
                if inspection.outcome == "ambiguous":
                    await self.session.rollback()
                    return PublicationAutodeleteViewsActionReserveResult(outcome="ambiguous")

            root = (
                _mapping(raw_root)
                if raw_root is not None
                else {
                    "version": _VIEWS_ACTIONS_VERSION,
                    "authority_fingerprint": fingerprint,
                    "telegram_chat_id": chat_id,
                    "telegram_message_ids": list(expected_ids),
                    "threshold": safe_threshold,
                    "observed_views": safe_observed_views,
                    "actions": {},
                }
            )
            if root is None:
                await self.session.rollback()
                return PublicationAutodeleteViewsActionReserveResult(outcome="conflict")
            actions = _mapping(root.get("actions"))
            if actions is None:
                await self.session.rollback()
                return PublicationAutodeleteViewsActionReserveResult(outcome="conflict")

            existing = _mapping(actions.get(str(message_id)))
            if existing is not None:
                state = str(existing.get("state", ""))
                await self.session.rollback()
                if state in _TERMINAL_STATES:
                    return PublicationAutodeleteViewsActionReserveResult(
                        outcome="already_terminal",
                        existing_state=state,
                    )
                return PublicationAutodeleteViewsActionReserveResult(
                    outcome="ambiguous",
                    existing_state=state,
                )

            reservation_token = uuid.uuid4().hex
            reservation = PublicationAutodeleteViewsActionReservation(
                publication_id=publication_id,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                authority_fingerprint=fingerprint,
                reservation_token=reservation_token,
                autodelete_lease_token=str(handle.lease_token),
                autodelete_lease_holder=str(handle.holder),
            )
            actions[str(message_id)] = {
                "telegram_chat_id": chat_id,
                "telegram_message_id": message_id,
                "authority_fingerprint": fingerprint,
                "reservation_token": reservation_token,
                "autodelete_lease_token": reservation.autodelete_lease_token,
                "autodelete_lease_holder": reservation.autodelete_lease_holder,
                "state": "reserved",
                "reserved_at": current.isoformat(),
            }
            root["actions"] = actions
            new_meta = deepcopy(meta)
            new_meta[AUTODELETE_VIEWS_ACTIONS_META_KEY] = root
            publication.meta = new_meta
            await self.session.commit()
            return PublicationAutodeleteViewsActionReserveResult(
                outcome="reserved",
                reservation=reservation,
                existing_state="reserved",
            )
        except Exception:
            await self.session.rollback()
            raise

    async def _finish(
        self,
        reservation: PublicationAutodeleteViewsActionReservation,
        *,
        state: Literal["succeeded", "unavailable", "unknown"],
        finished_at: datetime | None = None,
    ) -> bool:
        identity = self._valid_identity(
            publication_id=reservation.publication_id,
            telegram_chat_id=reservation.telegram_chat_id,
            telegram_message_id=reservation.telegram_message_id,
            authority_fingerprint=reservation.authority_fingerprint,
            telegram_message_ids=(reservation.telegram_message_id,),
            threshold=1,
            observed_views=1,
        )
        if (
            identity is None
            or not str(reservation.reservation_token)
            or not str(reservation.autodelete_lease_token)
            or not str(reservation.autodelete_lease_holder)
        ):
            return False
        publication_id, chat_id, message_id, fingerprint, _, _, _ = identity
        current = _utc(finished_at)

        try:
            publication = (
                await self.session.execute(
                    select(Publication)
                    .where(Publication.id == publication_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if publication is None:
                await self.session.rollback()
                return False
            meta = _mapping(publication.meta)
            if meta is None:
                await self.session.rollback()
                return False
            root = _mapping(meta.get(AUTODELETE_VIEWS_ACTIONS_META_KEY))
            actions = _mapping(root.get("actions")) if root is not None else None
            action = _mapping(actions.get(str(message_id))) if actions is not None else None
            if root is None or action is None:
                await self.session.rollback()
                return False

            exact = (
                root.get("version") == _VIEWS_ACTIONS_VERSION
                and str(root.get("authority_fingerprint", "")) == fingerprint
                and root.get("telegram_chat_id") == chat_id
                and action.get("telegram_chat_id") == chat_id
                and action.get("telegram_message_id") == message_id
                and str(action.get("authority_fingerprint", "")) == fingerprint
                and str(action.get("reservation_token", ""))
                == str(reservation.reservation_token)
                and str(action.get("autodelete_lease_token", ""))
                == str(reservation.autodelete_lease_token)
                and str(action.get("autodelete_lease_holder", ""))
                == str(reservation.autodelete_lease_holder)
            )
            if not exact:
                await self.session.rollback()
                return False

            current_state = str(action.get("state", ""))
            if current_state == state:
                await self.session.rollback()
                return True
            if current_state != "reserved":
                await self.session.rollback()
                return False

            new_action = dict(action)
            new_action["state"] = state
            new_action["finished_at"] = current.isoformat()
            new_actions = dict(actions)
            new_actions[str(message_id)] = new_action
            new_root = dict(root)
            new_root["actions"] = new_actions
            new_meta = deepcopy(meta)
            new_meta[AUTODELETE_VIEWS_ACTIONS_META_KEY] = new_root
            publication.meta = new_meta
            await self.session.commit()
            return True
        except Exception:
            await self.session.rollback()
            raise

    async def mark_succeeded(
        self,
        reservation: PublicationAutodeleteViewsActionReservation,
        *,
        finished_at: datetime | None = None,
    ) -> bool:
        return await self._finish(reservation, state="succeeded", finished_at=finished_at)

    async def mark_unavailable(
        self,
        reservation: PublicationAutodeleteViewsActionReservation,
        *,
        finished_at: datetime | None = None,
    ) -> bool:
        return await self._finish(reservation, state="unavailable", finished_at=finished_at)

    async def mark_unknown(
        self,
        reservation: PublicationAutodeleteViewsActionReservation,
        *,
        finished_at: datetime | None = None,
    ) -> bool:
        return await self._finish(reservation, state="unknown", finished_at=finished_at)
