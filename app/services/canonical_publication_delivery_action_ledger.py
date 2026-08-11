from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_delivery import (
    PublicationDeliveryAction,
    PublicationDeliveryLease,
)
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryLeaseHandle,
)


_ACTION_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,159}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTION_TYPES = {"pin", "forward"}


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryActionReservation:
    publication_id: int
    action_key: str
    action_type: Literal["pin", "forward"]
    intent_fingerprint: str
    delivery_lease_token: str


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryActionReserveResult:
    outcome: Literal["reserved", "already_reserved", "conflict", "ineligible"]
    reservation: CanonicalPublicationDeliveryActionReservation | None = None
    existing_state: str | None = None


class CanonicalPublicationDeliveryActionLedger:
    """Reserve non-idempotent delivery auxiliaries before provider invocation.

    `reserved` is a one-way no-retry barrier. Only the caller that receives a *new*
    `reserved` outcome may invoke the provider. Existing rows never authorize another
    call, regardless of whether their state is still reserved, succeeded, or unknown.

    Reservation itself requires the exact still-live primary delivery lease and the
    canonical `sending` lifecycle. Terminal action state may be recorded after provider
    completion without requiring the delivery lease to remain live; that write records
    evidence only and can never authorize another provider call.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _normalized_identity(
        *,
        action_key: str,
        action_type: str,
        intent_fingerprint: str,
    ) -> tuple[str, Literal["pin", "forward"], str] | None:
        key = str(action_key).strip().lower()
        action = str(action_type).strip().lower()
        fingerprint = str(intent_fingerprint).strip().lower()
        if action not in _ACTION_TYPES:
            return None
        if not _ACTION_KEY_RE.fullmatch(key):
            return None
        if not key.startswith(f"{action}:"):
            return None
        if not _FINGERPRINT_RE.fullmatch(fingerprint):
            return None
        return key, action, fingerprint  # type: ignore[return-value]

    async def _live_lifecycle(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        at: datetime,
    ) -> tuple[PublicationDeliveryLease, Publication, ScheduleEntry, PublicationAttempt] | None:
        try:
            publication_id = int(handle.publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        token = str(handle.lease_token)
        if publication_id <= 0 or not token:
            return None

        lease = (
            await self.session.execute(
                select(PublicationDeliveryLease)
                .where(
                    PublicationDeliveryLease.publication_id == publication_id,
                    PublicationDeliveryLease.lease_token == token,
                    PublicationDeliveryLease.expires_at > at,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            return None

        publication = (
            await self.session.execute(
                select(Publication)
                .where(Publication.id == publication_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            publication is None
            or publication.status != "sending"
            or int(publication.attempt_count or 0) != 1
            or publication.schedule_entry_id is None
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
        ):
            return None

        schedule = (
            await self.session.execute(
                select(ScheduleEntry)
                .where(
                    ScheduleEntry.id == int(publication.schedule_entry_id),
                    ScheduleEntry.channel_id == int(publication.channel_id),
                    ScheduleEntry.content_item_id == int(publication.content_item_id),
                    ScheduleEntry.content_revision == int(publication.content_revision),
                    ScheduleEntry.status == "pending",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if schedule is None:
            return None

        attempt = (
            await self.session.execute(
                select(PublicationAttempt)
                .where(
                    PublicationAttempt.publication_id == publication_id,
                    PublicationAttempt.attempt == 1,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            attempt is None
            or attempt.status != "sending"
            or attempt.finished_at is not None
            or attempt.telegram_message_ids is not None
            or attempt.error is not None
            or dict(attempt.meta or {}).get("canonical_delivery") is not True
        ):
            return None
        return lease, publication, schedule, attempt

    async def reserve(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        action_key: str,
        action_type: str,
        intent_fingerprint: str,
        now: datetime | None = None,
    ) -> CanonicalPublicationDeliveryActionReserveResult:
        identity = self._normalized_identity(
            action_key=action_key,
            action_type=action_type,
            intent_fingerprint=intent_fingerprint,
        )
        if identity is None:
            return CanonicalPublicationDeliveryActionReserveResult(outcome="ineligible")
        key, action, fingerprint = identity
        current = _utc(now)

        try:
            lifecycle = await self._live_lifecycle(handle, at=current)
            if lifecycle is None:
                await self.session.rollback()
                return CanonicalPublicationDeliveryActionReserveResult(outcome="ineligible")

            existing = (
                await self.session.execute(
                    select(PublicationDeliveryAction)
                    .where(
                        PublicationDeliveryAction.publication_id
                        == int(handle.publication_id),
                        PublicationDeliveryAction.action_key == key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing_action = str(existing.action_type)
                existing_fingerprint = str(existing.intent_fingerprint)
                existing_state = str(existing.state)
                await self.session.rollback()
                exact = (
                    existing_action == action
                    and existing_fingerprint == fingerprint
                )
                return CanonicalPublicationDeliveryActionReserveResult(
                    outcome="already_reserved" if exact else "conflict",
                    existing_state=existing_state,
                )

            reservation = CanonicalPublicationDeliveryActionReservation(
                publication_id=int(handle.publication_id),
                action_key=key,
                action_type=action,
                intent_fingerprint=fingerprint,
                delivery_lease_token=str(handle.lease_token),
            )
            self.session.add(
                PublicationDeliveryAction(
                    publication_id=reservation.publication_id,
                    action_key=reservation.action_key,
                    action_type=reservation.action_type,
                    state="reserved",
                    intent_fingerprint=reservation.intent_fingerprint,
                    reserved_by_lease_token=reservation.delivery_lease_token,
                )
            )
            await self.session.commit()
            return CanonicalPublicationDeliveryActionReserveResult(
                outcome="reserved",
                reservation=reservation,
                existing_state="reserved",
            )
        except IntegrityError:
            await self.session.rollback()
            existing = (
                await self.session.execute(
                    select(PublicationDeliveryAction).where(
                        PublicationDeliveryAction.publication_id
                        == int(handle.publication_id),
                        PublicationDeliveryAction.action_key == key,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                return CanonicalPublicationDeliveryActionReserveResult(outcome="conflict")
            exact = (
                str(existing.action_type) == action
                and str(existing.intent_fingerprint) == fingerprint
            )
            return CanonicalPublicationDeliveryActionReserveResult(
                outcome="already_reserved" if exact else "conflict",
                existing_state=str(existing.state),
            )
        except Exception:
            await self.session.rollback()
            raise

    async def _finish(
        self,
        reservation: CanonicalPublicationDeliveryActionReservation,
        *,
        state: Literal["succeeded", "unknown"],
        finished_at: datetime | None,
    ) -> bool:
        identity = self._normalized_identity(
            action_key=reservation.action_key,
            action_type=reservation.action_type,
            intent_fingerprint=reservation.intent_fingerprint,
        )
        if identity is None or not str(reservation.delivery_lease_token):
            return False
        key, action, fingerprint = identity
        current = _utc(finished_at)
        try:
            result = await self.session.execute(
                update(PublicationDeliveryAction)
                .where(
                    PublicationDeliveryAction.publication_id
                    == int(reservation.publication_id),
                    PublicationDeliveryAction.action_key == key,
                    PublicationDeliveryAction.action_type == action,
                    PublicationDeliveryAction.intent_fingerprint == fingerprint,
                    PublicationDeliveryAction.reserved_by_lease_token
                    == str(reservation.delivery_lease_token),
                    PublicationDeliveryAction.state == "reserved",
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

        existing = await self.session.get(
            PublicationDeliveryAction,
            {
                "publication_id": int(reservation.publication_id),
                "action_key": key,
            },
        )
        return bool(
            existing is not None
            and str(existing.action_type) == action
            and str(existing.intent_fingerprint) == fingerprint
            and str(existing.reserved_by_lease_token)
            == str(reservation.delivery_lease_token)
            and str(existing.state) == state
        )

    async def mark_succeeded(
        self,
        reservation: CanonicalPublicationDeliveryActionReservation,
        *,
        finished_at: datetime | None = None,
    ) -> bool:
        return await self._finish(
            reservation,
            state="succeeded",
            finished_at=finished_at,
        )

    async def mark_unknown(
        self,
        reservation: CanonicalPublicationDeliveryActionReservation,
        *,
        finished_at: datetime | None = None,
    ) -> bool:
        return await self._finish(
            reservation,
            state="unknown",
            finished_at=finished_at,
        )
