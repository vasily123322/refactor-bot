from __future__ import annotations

from collections.abc import Mapping

from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_publication_atomic_claim_outcome import (
    CanonicalPublicationAtomicClaimFailureClassifier,
)
from app.services.canonical_publication_delivery_handoff_executor import (
    CanonicalPublicationDeliveryHandoffExecutionResult,
    CanonicalPublicationDeliveryHandoffExecutor,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)


def _positive_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _repeat_enabled(schedule: ScheduleEntry | None) -> bool:
    if schedule is None or not isinstance(schedule.repeat_rule, Mapping):
        return False
    rule = dict(schedule.repeat_rule)
    return (
        rule.get("enabled") is True
        and _positive_int(rule.get("seconds")) is not None
    )


class CanonicalPublicationRepeatHandoffExecutor(CanonicalPublicationDeliveryHandoffExecutor):
    """Route linked fixed-delay repeat through its dedicated atomic authority seam.

    Every repeat/effect composition fact is forwarded independently. Plain repeat+time,
    time+pin and ordered time+forward therefore carry distinct explicit bits.
    """

    async def execute(self, publication_id: int):
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_publication_id = 0
        if safe_publication_id <= 0:
            return CanonicalPublicationDeliveryHandoffExecutionResult(
                outcome="ineligible",
                handoff_outcome="invalid_publication",
            )

        async with self.session_factory() as session:
            publication = await session.get(Publication, safe_publication_id)
            if publication is None:
                return CanonicalPublicationDeliveryHandoffExecutionResult(
                    outcome="ineligible",
                    handoff_outcome="missing_publication",
                )
            linked = publication.legacy_post_task_id is not None
            schedule = (
                await session.get(ScheduleEntry, int(publication.schedule_entry_id))
                if linked and publication.schedule_entry_id is not None
                else None
            )
            linked_repeat = linked and _repeat_enabled(schedule)

        if not linked_repeat:
            return await super().execute(safe_publication_id)

        allow_repeat = bool(getattr(self.executor, "allow_repeat", False))
        allow_time_autodelete = bool(
            getattr(self.executor, "allow_time_autodelete", False)
        )
        allow_repeat_time = bool(getattr(self.executor, "allow_repeat_time", False))
        allow_repeat_time_pin = bool(
            getattr(self.executor, "allow_repeat_time_pin", False)
        )
        allow_repeat_time_forward = bool(
            getattr(self.executor, "allow_repeat_time_forward", False)
        )
        allow_views_autodelete = bool(
            getattr(self.executor, "allow_views_autodelete", False)
        )
        allow_repeat_views = bool(
            getattr(self.executor, "allow_repeat_views", False)
        )
        allow_repeat_views_pin = bool(
            getattr(self.executor, "allow_repeat_views_pin", False)
        )
        allow_repeat_views_forward = bool(
            getattr(self.executor, "allow_repeat_views_forward", False)
        )
        allow_repeat_views_pin_forward = bool(
            getattr(self.executor, "allow_repeat_views_pin_forward", False)
        )
        async with self.session_factory() as session:
            transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                session
            ).claim_linked_repeat(
                safe_publication_id,
                holder=str(self.executor.holder),
                ttl_seconds=int(self.executor.lease_seconds),
                allow_repeat=allow_repeat,
                allow_time_autodelete=allow_time_autodelete,
                allow_repeat_time=allow_repeat_time,
                allow_repeat_time_pin=allow_repeat_time_pin,
                allow_repeat_time_forward=allow_repeat_time_forward,
                allow_views_autodelete=allow_views_autodelete,
                allow_repeat_views=allow_repeat_views,
                allow_repeat_views_pin=allow_repeat_views_pin,
                allow_repeat_views_forward=allow_repeat_views_forward,
                allow_repeat_views_pin_forward=allow_repeat_views_pin_forward,
            )

        if transfer.outcome == "claim_unavailable":
            task_id = transfer.legacy_post_task_id
            if task_id is None:
                return CanonicalPublicationDeliveryHandoffExecutionResult(
                    outcome="lease_lost",
                    handoff_outcome="claim_unavailable",
                )
            async with self.session_factory() as session:
                classification = await CanonicalPublicationAtomicClaimFailureClassifier(
                    session
                ).classify(
                    publication_id=safe_publication_id,
                    legacy_post_task_id=int(task_id),
                )
            if classification.outcome == "claim_rejected":
                return CanonicalPublicationDeliveryHandoffExecutionResult(
                    outcome="ineligible",
                    handoff_outcome="claim_rejected",
                )
            return CanonicalPublicationDeliveryHandoffExecutionResult(
                outcome="lease_lost",
                handoff_outcome="claim_unavailable",
            )

        if transfer.outcome != "claimed" or transfer.claim is None:
            return CanonicalPublicationDeliveryHandoffExecutionResult(
                outcome="ineligible",
                handoff_outcome=str(transfer.outcome),
            )

        return await self.executor.execute_claim(transfer.claim)
