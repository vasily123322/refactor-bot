from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_repeat_plan_reservation import (
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_successor_materializer import (
    CanonicalRepeatSuccessorMaterializer,
)
from app.services.canonical_repeat_successor_verifier import (
    CanonicalRepeatSuccessorVerifier,
)


@dataclass(frozen=True, slots=True)
class CanonicalRepeatTransitionResult:
    source_publication_id: int
    outcome: Literal[
        "created",
        "existing",
        "existing_transport",
        "ineligible",
        "conflict",
    ]
    successor_publication_id: int | None = None
    successor_schedule_entry_id: int | None = None
    successor_legacy_post_task_id: int | None = None


class CanonicalRepeatTransitionCoordinator:
    """Idempotently drive one successful-repeat transition in canonical state.

    Existing reservation/successor state is verified before any new reservation write.
    A missing reservation is created from the pure repeat planner, then the transport-
    free successor materializer is invoked and the resulting occurrence is verified.
    No legacy repeat creator or ``PostTask`` write exists in this coordinator.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _from_verification(source_publication_id: int, verification):
        if verification.outcome == "matched_canonical":
            return CanonicalRepeatTransitionResult(
                source_publication_id=source_publication_id,
                outcome="existing",
                successor_publication_id=verification.successor_publication_id,
                successor_schedule_entry_id=verification.successor_schedule_entry_id,
                successor_legacy_post_task_id=None,
            )
        if verification.outcome == "matched_transport":
            return CanonicalRepeatTransitionResult(
                source_publication_id=source_publication_id,
                outcome="existing_transport",
                successor_publication_id=verification.successor_publication_id,
                successor_schedule_entry_id=verification.successor_schedule_entry_id,
                successor_legacy_post_task_id=verification.successor_legacy_post_task_id,
            )
        return None

    async def transition(
        self,
        source_publication_id: int,
        *,
        after: datetime | None = None,
    ) -> CanonicalRepeatTransitionResult:
        try:
            safe_source_id = int(source_publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_source_id = 0
        if safe_source_id <= 0:
            return CanonicalRepeatTransitionResult(safe_source_id, "ineligible")

        verifier = CanonicalRepeatSuccessorVerifier(self.session)
        before = await verifier.verify(safe_source_id)
        existing = self._from_verification(safe_source_id, before)
        if existing is not None:
            return existing
        if before.outcome == "conflict":
            return CanonicalRepeatTransitionResult(safe_source_id, "conflict")

        if before.outcome == "ineligible":
            reservation = await CanonicalRepeatPlanReservationService(
                self.session
            ).reserve_next(
                safe_source_id,
                after=after,
            )
            if reservation.outcome == "ineligible":
                return CanonicalRepeatTransitionResult(safe_source_id, "ineligible")
            if reservation.outcome in {"conflict", "existing_successor"}:
                return CanonicalRepeatTransitionResult(safe_source_id, "conflict")
            if reservation.outcome not in {"reserved", "already_reserved"}:
                return CanonicalRepeatTransitionResult(safe_source_id, "conflict")
        elif before.outcome != "pending":
            return CanonicalRepeatTransitionResult(safe_source_id, "conflict")

        materialized = await CanonicalRepeatSuccessorMaterializer(
            self.session
        ).materialize(safe_source_id)
        if materialized.outcome == "ineligible":
            return CanonicalRepeatTransitionResult(safe_source_id, "ineligible")
        if materialized.outcome == "conflict":
            return CanonicalRepeatTransitionResult(safe_source_id, "conflict")

        after_verification = await verifier.verify(safe_source_id)
        if materialized.outcome in {"created", "existing"}:
            if (
                after_verification.outcome != "matched_canonical"
                or after_verification.successor_publication_id
                != materialized.publication_id
                or after_verification.successor_schedule_entry_id
                != materialized.schedule_entry_id
                or after_verification.successor_legacy_post_task_id is not None
            ):
                return CanonicalRepeatTransitionResult(safe_source_id, "conflict")
            return CanonicalRepeatTransitionResult(
                source_publication_id=safe_source_id,
                outcome=("created" if materialized.outcome == "created" else "existing"),
                successor_publication_id=materialized.publication_id,
                successor_schedule_entry_id=materialized.schedule_entry_id,
                successor_legacy_post_task_id=None,
            )

        if materialized.outcome == "existing_transport":
            if (
                after_verification.outcome != "matched_transport"
                or after_verification.successor_publication_id
                != materialized.publication_id
                or after_verification.successor_schedule_entry_id
                != materialized.schedule_entry_id
                or after_verification.successor_legacy_post_task_id
                != materialized.legacy_post_task_id
            ):
                return CanonicalRepeatTransitionResult(safe_source_id, "conflict")
            return CanonicalRepeatTransitionResult(
                source_publication_id=safe_source_id,
                outcome="existing_transport",
                successor_publication_id=materialized.publication_id,
                successor_schedule_entry_id=materialized.schedule_entry_id,
                successor_legacy_post_task_id=materialized.legacy_post_task_id,
            )

        return CanonicalRepeatTransitionResult(safe_source_id, "conflict")
