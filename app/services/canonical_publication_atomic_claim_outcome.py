from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt


@dataclass(frozen=True, slots=True)
class CanonicalPublicationAtomicClaimFailureClassification:
    publication_id: int
    legacy_post_task_id: int
    outcome: Literal["claim_rejected", "claim_unavailable"]


class CanonicalPublicationAtomicClaimFailureClassifier:
    """Classify a failed linked atomic claim from its durable post-rollback state.

    A capability claim may return no executable handle for two fundamentally different
    reasons:

    - it rejected before the authority commit, in which case rollback must have restored
      the exact `queued + linked pending PostTask` state and there must be no canonical
      attempt or delivery lease;
    - authority was already committed and a later snapshot/renew step failed, or some
      other partial state exists. That state is never considered retryable and remains
      owned by fail-closed canonical recovery.

    Only the complete first state is classified `claim_rejected`. Every incomplete,
    ambiguous or already-committed state is `claim_unavailable`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def classify(
        self,
        *,
        publication_id: int,
        legacy_post_task_id: int,
    ) -> CanonicalPublicationAtomicClaimFailureClassification:
        safe_publication_id = int(publication_id)
        safe_task_id = int(legacy_post_task_id)

        # Close any failed/prepared transaction first. Reads below then describe only
        # durable state visible to a fresh transaction, never the abandoned unit of work.
        await self.session.rollback()

        publication = await self.session.get(Publication, safe_publication_id)
        task = await self.session.get(PostTask, safe_task_id)
        lease = await self.session.get(PublicationDeliveryLease, safe_publication_id)
        attempt_id = (
            await self.session.execute(
                select(PublicationAttempt.id)
                .where(PublicationAttempt.publication_id == safe_publication_id)
                .limit(1)
            )
        ).scalar_one_or_none()

        exact_rollback = bool(
            publication is not None
            and publication.status == "queued"
            and int(publication.attempt_count or 0) == 0
            and publication.legacy_post_task_id is not None
            and int(publication.legacy_post_task_id) == safe_task_id
            and publication.telegram_message_ids in (None, [])
            and publication.result_link is None
            and publication.last_error is None
            and task is not None
            and task.status == "pending"
            and lease is None
            and attempt_id is None
        )
        await self.session.rollback()

        return CanonicalPublicationAtomicClaimFailureClassification(
            publication_id=safe_publication_id,
            legacy_post_task_id=safe_task_id,
            outcome="claim_rejected" if exact_rollback else "claim_unavailable",
        )
