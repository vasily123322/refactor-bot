from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_autodelete import (
    PublicationAutodeleteAction,
    PublicationAutodeleteLease,
)
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle


_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATES = {"succeeded", "unavailable"}
_AMBIGUOUS_STATES = {"reserved", "unknown"}


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteActionReservation:
    publication_id: int
    telegram_chat_id: int
    telegram_message_id: int
    authority_fingerprint: str
    reservation_token: str
    autodelete_lease_token: str


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteActionReserveResult:
    outcome: Literal[
        "reserved",
        "already_terminal",
        "ambiguous",
        "conflict",
        "ineligible",
    ]
    reservation: PublicationAutodeleteActionReservation | None = None
    existing_state: str | None = None


class PublicationAutodeleteActionLedger:
    """One-way, per-message authority ledger for Telegram deletion.

    Only a newly committed ``reserved`` row authorizes one provider invocation.
    Existing ``reserved``/``unknown`` rows never authorize replay. Existing terminal
    rows may be reused as evidence for the exact same authority fingerprint, allowing
    a multi-message batch to continue after a clean crash between messages without
    repeating already-resolved deletes.
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
        return (
            safe_publication_id,
            safe_chat_id,
            safe_message_id,
            fingerprint,
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
        if publication_id <= 0 or not token:
            return None
        return (
            await self.session.execute(
                select(PublicationAutodeleteLease)
                .where(
                    PublicationAutodeleteLease.publication_id == publication_id,
                    PublicationAutodeleteLease.lease_token == token,
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
        now: datetime | None = None,
    ) -> PublicationAutodeleteActionReserveResult:
        identity = self._valid_identity(
            publication_id=handle.publication_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            authority_fingerprint=authority_fingerprint,
        )
        if identity is None:
            return PublicationAutodeleteActionReserveResult(outcome="ineligible")
        publication_id, chat_id, message_id, fingerprint = identity
        current = _utc(now)

        try:
            lease = await self._live_lease(handle, at=current)
            if lease is None:
                await self.session.rollback()
                return PublicationAutodeleteActionReserveResult(outcome="ineligible")

            rows = (
                await self.session.execute(
                    select(PublicationAutodeleteAction)
                    .where(
                        PublicationAutodeleteAction.publication_id == publication_id
                    )
                    .with_for_update()
                )
            ).scalars().all()

            if any(
                str(row.authority_fingerprint) != fingerprint
                for row in rows
            ):
                await self.session.rollback()
                return PublicationAutodeleteActionReserveResult(outcome="conflict")

            if any(str(row.state) in _AMBIGUOUS_STATES for row in rows):
                await self.session.rollback()
                return PublicationAutodeleteActionReserveResult(outcome="ambiguous")

            existing = next(
                (
                    row
                    for row in rows
                    if int(row.telegram_message_id) == message_id
                ),
                None,
            )
            if existing is not None:
                exact = (
                    int(existing.telegram_chat_id) == chat_id
                    and str(existing.authority_fingerprint) == fingerprint
                )
                state = str(existing.state)
                await self.session.rollback()
                if not exact:
                    return PublicationAutodeleteActionReserveResult(
                        outcome="conflict",
                        existing_state=state,
                    )
                if state in _TERMINAL_STATES:
                    return PublicationAutodeleteActionReserveResult(
                        outcome="already_terminal",
                        existing_state=state,
                    )
                return PublicationAutodeleteActionReserveResult(
                    outcome="ambiguous",
                    existing_state=state,
                )

            reservation_token = uuid.uuid4().hex
            reservation = PublicationAutodeleteActionReservation(
                publication_id=publication_id,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                authority_fingerprint=fingerprint,
                reservation_token=reservation_token,
                autodelete_lease_token=str(handle.lease_token),
            )
            self.session.add(
                PublicationAutodeleteAction(
                    publication_id=publication_id,
                    telegram_chat_id=chat_id,
                    telegram_message_id=message_id,
                    authority_fingerprint=fingerprint,
                    reservation_token=reservation_token,
                    reserved_by_lease_token=reservation.autodelete_lease_token,
                    state="reserved",
                )
            )
            await self.session.commit()
            return PublicationAutodeleteActionReserveResult(
                outcome="reserved",
                reservation=reservation,
                existing_state="reserved",
            )
        except IntegrityError:
            await self.session.rollback()
            existing = (
                await self.session.execute(
                    select(PublicationAutodeleteAction).where(
                        PublicationAutodeleteAction.publication_id == publication_id,
                        PublicationAutodeleteAction.telegram_message_id == message_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                return PublicationAutodeleteActionReserveResult(outcome="conflict")
            exact = (
                int(existing.telegram_chat_id) == chat_id
                and str(existing.authority_fingerprint) == fingerprint
            )
            state = str(existing.state)
            if not exact:
                return PublicationAutodeleteActionReserveResult(
                    outcome="conflict",
                    existing_state=state,
                )
            if state in _TERMINAL_STATES:
                return PublicationAutodeleteActionReserveResult(
                    outcome="already_terminal",
                    existing_state=state,
                )
            return PublicationAutodeleteActionReserveResult(
                outcome="ambiguous",
                existing_state=state,
            )
        except Exception:
            await self.session.rollback()
            raise

    async def _finish(
        self,
        reservation: PublicationAutodeleteActionReservation,
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
            or not str(reservation.reservation_token)
            or not str(reservation.autodelete_lease_token)
        ):
            return False
        publication_id, chat_id, message_id, fingerprint = identity
        current = _utc(finished_at)
        try:
            result = await self.session.execute(
                update(PublicationAutodeleteAction)
                .where(
                    PublicationAutodeleteAction.publication_id == publication_id,
                    PublicationAutodeleteAction.telegram_message_id == message_id,
                    PublicationAutodeleteAction.telegram_chat_id == chat_id,
                    PublicationAutodeleteAction.authority_fingerprint == fingerprint,
                    PublicationAutodeleteAction.reservation_token
                    == str(reservation.reservation_token),
                    PublicationAutodeleteAction.reserved_by_lease_token
                    == str(reservation.autodelete_lease_token),
                    PublicationAutodeleteAction.state == "reserved",
                )
                .values(state=state, finished_at=current)
                .execution_options(synchronize_session=False)
            )
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        if int(result.rowcount or 0) == 1:
            return True

        existing = (
            await self.session.execute(
                select(PublicationAutodeleteAction).where(
                    PublicationAutodeleteAction.publication_id == publication_id,
                    PublicationAutodeleteAction.telegram_message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        return bool(
            existing is not None
            and int(existing.telegram_chat_id) == chat_id
            and str(existing.authority_fingerprint) == fingerprint
            and str(existing.reservation_token) == str(reservation.reservation_token)
            and str(existing.reserved_by_lease_token)
            == str(reservation.autodelete_lease_token)
            and str(existing.state) == state
        )

    async def mark_succeeded(
        self,
        reservation: PublicationAutodeleteActionReservation,
        *,
        finished_at: datetime | None = None,
    ) -> bool:
        return await self._finish(
            reservation,
            state="succeeded",
            finished_at=finished_at,
        )

    async def mark_unavailable(
        self,
        reservation: PublicationAutodeleteActionReservation,
        *,
        finished_at: datetime | None = None,
    ) -> bool:
        return await self._finish(
            reservation,
            state="unavailable",
            finished_at=finished_at,
        )

    async def mark_unknown(
        self,
        reservation: PublicationAutodeleteActionReservation,
        *,
        finished_at: datetime | None = None,
    ) -> bool:
        return await self._finish(
            reservation,
            state="unknown",
            finished_at=finished_at,
        )
