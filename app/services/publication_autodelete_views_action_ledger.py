from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_autodelete import PublicationAutodeleteLease
from app.domain.publishing.models import Publication
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle


VIEWS_ACTION_LEDGER_META_KEY = "autodelete_views_actions"
VIEWS_ACTION_LEDGER_VERSION = 1
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATES = {"succeeded", "unavailable"}
_AMBIGUOUS_STATES = {"reserved", "unknown"}
_ALL_STATES = _TERMINAL_STATES | _AMBIGUOUS_STATES


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def views_action_authority_fingerprint(payload: Mapping[str, Any]) -> str | None:
    """Return the deterministic SHA-256 identity used by destructive views actions.

    This deliberately mirrors the destructive-action contract used by time autodelete
    without depending on that branch's schema. The storage backend may converge later;
    callers rely only on this deterministic authority identity plus reservation tokens.
    """

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
class PublicationAutodeleteViewsActionSnapshot:
    outcome: Literal["empty", "partial", "terminal", "ambiguous", "conflict"]
    succeeded_count: int = 0
    unavailable_count: int = 0


class PublicationAutodeleteViewsActionLedger:
    """Schema-free durable destructive-action contract for views autodelete.

    The current storage backend is occurrence-local ``Publication.meta`` so the repeat
    stack does not create an Alembic head competing with the independent time-autodelete
    branch. Semantics intentionally match that branch's action ledger:

    * only a newly committed ``reserved`` action authorizes one Telegram DELETE;
    * ``reserved`` and ``unknown`` are permanent automatic no-replay barriers;
    * terminal actions can be skipped after a clean partial completion;
    * immutable chat/message/fingerprint plus reservation and lease provenance are
      required for every finalization.

    The public methods form a convergence seam: the storage implementation can later be
    replaced by the shared table after the independent schema lands without changing the
    destructive state machine used by callers.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _safe_ids(values: Sequence[int]) -> tuple[int, ...] | None:
        result: list[int] = []
        seen: set[int] = set()
        for value in values:
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if parsed <= 0 or parsed in seen:
                return None
            seen.add(parsed)
            result.append(parsed)
        return tuple(result) if result else None

    @staticmethod
    def _valid_identity(
        *,
        publication_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        authority_fingerprint: str,
    ) -> tuple[int, int, int, str] | None:
        try:
            safe_publication_id = int(publication_id)
            safe_chat_id = int(telegram_chat_id)
            safe_message_id = int(telegram_message_id)
        except (TypeError, ValueError, OverflowError):
            return None
        fingerprint = str(authority_fingerprint).strip().lower()
        if (
            safe_publication_id <= 0
            or safe_chat_id == 0
            or safe_message_id <= 0
            or not _FINGERPRINT_RE.fullmatch(fingerprint)
        ):
            return None
        return safe_publication_id, safe_chat_id, safe_message_id, fingerprint

    @staticmethod
    def _raw_meta(publication: Publication) -> dict[str, Any] | None:
        raw = publication.meta
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            return None
        return {str(key): value for key, value in raw.items()}

    @staticmethod
    def _validate_ledger(
        raw: Any,
        *,
        publication_id: int,
        telegram_chat_id: int,
        expected_message_ids: tuple[int, ...],
        authority_fingerprint: str,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
        if not isinstance(raw, Mapping):
            return None, None
        ledger = {str(key): value for key, value in raw.items()}
        try:
            version = int(ledger.get("version"))
            ledger_publication_id = int(ledger.get("publication_id"))
            ledger_chat_id = int(ledger.get("telegram_chat_id"))
        except (TypeError, ValueError, OverflowError):
            return None, None
        raw_ids = ledger.get("telegram_message_ids")
        if not isinstance(raw_ids, list):
            return None, None
        ids = PublicationAutodeleteViewsActionLedger._safe_ids(raw_ids)
        if (
            version != VIEWS_ACTION_LEDGER_VERSION
            or ledger_publication_id != publication_id
            or ledger_chat_id != telegram_chat_id
            or ids != expected_message_ids
            or str(ledger.get("authority_fingerprint", "")) != authority_fingerprint
        ):
            return None, None

        raw_actions = ledger.get("actions")
        if not isinstance(raw_actions, list):
            return None, None
        actions: list[dict[str, Any]] = []
        action_order: list[int] = []
        seen: set[int] = set()
        expected = set(expected_message_ids)
        for raw_action in raw_actions:
            if not isinstance(raw_action, Mapping):
                return None, None
            action = {str(key): value for key, value in raw_action.items()}
            try:
                action_publication_id = int(action.get("publication_id"))
                action_chat_id = int(action.get("telegram_chat_id"))
                message_id = int(action.get("telegram_message_id"))
            except (TypeError, ValueError, OverflowError):
                return None, None
            state = str(action.get("state", ""))
            if (
                action_publication_id != publication_id
                or action_chat_id != telegram_chat_id
                or message_id not in expected
                or message_id in seen
                or str(action.get("authority_fingerprint", "")) != authority_fingerprint
                or state not in _ALL_STATES
                or not str(action.get("reservation_token", ""))
                or not str(action.get("autodelete_lease_token", ""))
                or not str(action.get("autodelete_lease_holder", ""))
            ):
                return None, None
            seen.add(message_id)
            action_order.append(message_id)
            actions.append(action)
        if action_order != list(expected_message_ids[: len(action_order)]):
            return None, None
        return ledger, actions

    async def _live_lease(
        self,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        publication_id: int,
        at: datetime,
    ) -> PublicationAutodeleteLease | None:
        try:
            handle_publication_id = int(handle.publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        token = str(handle.lease_token)
        holder = str(handle.holder)
        if handle_publication_id != publication_id or not token or not holder:
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

    async def has_live_lease_locked(
        self,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        publication_id: int,
        now: datetime | None = None,
    ) -> bool:
        return (
            await self._live_lease(
                handle,
                publication_id=int(publication_id),
                at=_utc(now),
            )
            is not None
        )

    def snapshot_locked(
        self,
        publication: Publication,
        *,
        telegram_chat_id: int,
        expected_message_ids: Sequence[int],
        authority_fingerprint: str,
    ) -> PublicationAutodeleteViewsActionSnapshot:
        identity = self._valid_identity(
            publication_id=int(publication.id),
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=int(expected_message_ids[0]) if expected_message_ids else 0,
            authority_fingerprint=authority_fingerprint,
        )
        ids = self._safe_ids(expected_message_ids)
        meta = self._raw_meta(publication)
        if identity is None or ids is None or meta is None:
            return PublicationAutodeleteViewsActionSnapshot(outcome="conflict")
        publication_id, chat_id, _, fingerprint = identity
        raw = meta.get(VIEWS_ACTION_LEDGER_META_KEY)
        if raw is None:
            return PublicationAutodeleteViewsActionSnapshot(outcome="empty")
        _, actions = self._validate_ledger(
            raw,
            publication_id=publication_id,
            telegram_chat_id=chat_id,
            expected_message_ids=ids,
            authority_fingerprint=fingerprint,
        )
        if actions is None:
            return PublicationAutodeleteViewsActionSnapshot(outcome="conflict")
        if any(str(action["state"]) in _AMBIGUOUS_STATES for action in actions):
            return PublicationAutodeleteViewsActionSnapshot(outcome="ambiguous")
        succeeded = sum(1 for action in actions if str(action["state"]) == "succeeded")
        unavailable = sum(1 for action in actions if str(action["state"]) == "unavailable")
        if len(actions) == len(ids):
            return PublicationAutodeleteViewsActionSnapshot(
                outcome="terminal",
                succeeded_count=succeeded,
                unavailable_count=unavailable,
            )
        return PublicationAutodeleteViewsActionSnapshot(
            outcome="partial",
            succeeded_count=succeeded,
            unavailable_count=unavailable,
        )

    async def reserve_locked(
        self,
        publication: Publication,
        handle: PublicationAutodeleteLeaseHandle,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
        expected_message_ids: Sequence[int],
        authority_fingerprint: str,
        now: datetime | None = None,
    ) -> PublicationAutodeleteViewsActionReserveResult:
        identity = self._valid_identity(
            publication_id=int(publication.id),
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            authority_fingerprint=authority_fingerprint,
        )
        ids = self._safe_ids(expected_message_ids)
        meta = self._raw_meta(publication)
        if identity is None or ids is None or meta is None:
            await self.session.rollback()
            return PublicationAutodeleteViewsActionReserveResult(outcome="ineligible")
        publication_id, chat_id, message_id, fingerprint = identity
        if message_id not in set(ids):
            await self.session.rollback()
            return PublicationAutodeleteViewsActionReserveResult(outcome="conflict")
        current = _utc(now)
        lease = await self._live_lease(
            handle,
            publication_id=publication_id,
            at=current,
        )
        if lease is None:
            await self.session.rollback()
            return PublicationAutodeleteViewsActionReserveResult(outcome="ineligible")

        raw = meta.get(VIEWS_ACTION_LEDGER_META_KEY)
        if raw is None:
            ledger: dict[str, Any] = {
                "version": VIEWS_ACTION_LEDGER_VERSION,
                "publication_id": publication_id,
                "telegram_chat_id": chat_id,
                "telegram_message_ids": list(ids),
                "authority_fingerprint": fingerprint,
                "actions": [],
            }
            actions: list[dict[str, Any]] = []
        else:
            parsed, parsed_actions = self._validate_ledger(
                raw,
                publication_id=publication_id,
                telegram_chat_id=chat_id,
                expected_message_ids=ids,
                authority_fingerprint=fingerprint,
            )
            if parsed is None or parsed_actions is None:
                await self.session.rollback()
                return PublicationAutodeleteViewsActionReserveResult(outcome="conflict")
            ledger = parsed
            actions = parsed_actions

        if any(str(action["state"]) in _AMBIGUOUS_STATES for action in actions):
            await self.session.rollback()
            return PublicationAutodeleteViewsActionReserveResult(outcome="ambiguous")

        existing = next(
            (action for action in actions if int(action["telegram_message_id"]) == message_id),
            None,
        )
        if existing is not None:
            state = str(existing["state"])
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
        action = {
            "publication_id": publication_id,
            "telegram_chat_id": chat_id,
            "telegram_message_id": message_id,
            "authority_fingerprint": fingerprint,
            "reservation_token": reservation_token,
            "autodelete_lease_token": reservation.autodelete_lease_token,
            "autodelete_lease_holder": reservation.autodelete_lease_holder,
            "state": "reserved",
            "reserved_at": current.isoformat(),
            "finished_at": None,
        }
        new_ledger = deepcopy(ledger)
        new_actions = [deepcopy(item) for item in actions]
        new_actions.append(action)
        new_ledger["actions"] = new_actions
        new_meta = deepcopy(meta)
        new_meta[VIEWS_ACTION_LEDGER_META_KEY] = new_ledger
        publication.meta = new_meta
        await self.session.commit()
        return PublicationAutodeleteViewsActionReserveResult(
            outcome="reserved",
            reservation=reservation,
            existing_state="reserved",
        )

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
        )
        if (
            identity is None
            or state not in {"succeeded", "unavailable", "unknown"}
            or not str(reservation.reservation_token)
            or not str(reservation.autodelete_lease_token)
            or not str(reservation.autodelete_lease_holder)
        ):
            return False
        publication_id, chat_id, message_id, fingerprint = identity
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
        meta = self._raw_meta(publication)
        if meta is None:
            await self.session.rollback()
            return False
        raw = meta.get(VIEWS_ACTION_LEDGER_META_KEY)
        if not isinstance(raw, Mapping):
            await self.session.rollback()
            return False
        ledger = {str(key): value for key, value in raw.items()}
        try:
            exact_ledger = (
                int(ledger.get("version", 0) or 0) == VIEWS_ACTION_LEDGER_VERSION
                and int(ledger.get("publication_id", 0) or 0) == publication_id
                and int(ledger.get("telegram_chat_id", 0) or 0) == chat_id
                and str(ledger.get("authority_fingerprint", "")) == fingerprint
            )
        except (TypeError, ValueError, OverflowError):
            exact_ledger = False
        if not exact_ledger:
            await self.session.rollback()
            return False
        raw_actions = ledger.get("actions")
        if not isinstance(raw_actions, list):
            await self.session.rollback()
            return False

        matched_index: int | None = None
        matched: dict[str, Any] | None = None
        for index, raw_action in enumerate(raw_actions):
            if not isinstance(raw_action, Mapping):
                await self.session.rollback()
                return False
            action = {str(key): value for key, value in raw_action.items()}
            try:
                candidate_message_id = int(action.get("telegram_message_id"))
            except (TypeError, ValueError, OverflowError):
                await self.session.rollback()
                return False
            if candidate_message_id == message_id:
                matched_index = index
                matched = action
                break
        if matched_index is None or matched is None:
            await self.session.rollback()
            return False

        try:
            exact = (
                int(matched.get("publication_id", 0) or 0) == publication_id
                and int(matched.get("telegram_chat_id", 0) or 0) == chat_id
                and str(matched.get("authority_fingerprint", "")) == fingerprint
                and str(matched.get("reservation_token", ""))
                == str(reservation.reservation_token)
                and str(matched.get("autodelete_lease_token", ""))
                == str(reservation.autodelete_lease_token)
                and str(matched.get("autodelete_lease_holder", ""))
                == str(reservation.autodelete_lease_holder)
            )
        except (TypeError, ValueError, OverflowError):
            exact = False
        if not exact:
            await self.session.rollback()
            return False
        existing_state = str(matched.get("state", ""))
        if existing_state == state:
            await self.session.rollback()
            return True
        if existing_state != "reserved":
            await self.session.rollback()
            return False

        new_action = deepcopy(matched)
        new_action["state"] = state
        new_action["finished_at"] = _utc(finished_at).isoformat()
        new_actions = [deepcopy(item) for item in raw_actions]
        new_actions[matched_index] = new_action
        new_ledger = deepcopy(ledger)
        new_ledger["actions"] = new_actions
        new_meta = deepcopy(meta)
        new_meta[VIEWS_ACTION_LEDGER_META_KEY] = new_ledger
        publication.meta = new_meta
        await self.session.commit()
        return True

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
