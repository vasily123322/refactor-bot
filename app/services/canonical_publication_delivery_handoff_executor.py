from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.domain.publishing.models import Publication
from app.services.canonical_publication_atomic_claim_outcome import (
    CanonicalPublicationAtomicClaimFailureClassifier,
)
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CanonicalPublicationAtomicHandoffClaimService,
)
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
)
from app.services.canonical_publication_linked_forward_atomic_handoff import (
    CanonicalPublicationLinkedForwardAtomicHandoffService,
)


class CanonicalPublicationDeliveryExecutorLike(Protocol):
    holder: str
    lease_seconds: int
    allow_time_autodelete: bool
    allow_views_autodelete: bool

    async def execute(self, publication_id: int): ...

    async def execute_claim(
        self,
        claim: CanonicalPublicationDeliveryClaim,
    ): ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryHandoffExecutionResult:
    outcome: str
    handoff_outcome: str


def _publication_requests_forward(publication: Publication) -> bool:
    meta = publication.meta
    if not isinstance(meta, Mapping):
        return False
    runtime_options = meta.get("runtime_options")
    return isinstance(runtime_options, Mapping) and "forward_to" in runtime_options


class CanonicalPublicationDeliveryHandoffExecutor:
    """Execute canonical-only rows directly and linked rows via atomic authority transfer.

    The general and forward-specific coordinators receive the same concrete time/views
    executor availability facts. A linked delete capability therefore cannot transfer
    authority unless its dependent canonical consumer actually started.

    If capability claim returns no executable handle, durable state is classified before
    choosing the wrapper outcome: complete rollback is ordinary `claim_rejected`; every
    partial/already-committed state is `claim_unavailable` and remains recovery-owned.
    """

    def __init__(
        self,
        *,
        executor: CanonicalPublicationDeliveryExecutorLike,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    ) -> None:
        self.executor = executor
        self.session_factory = session_factory

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
            forward_requested = linked and _publication_requests_forward(publication)

        if not linked:
            return await self.executor.execute(safe_publication_id)

        allow_time_autodelete = bool(self.executor.allow_time_autodelete)
        allow_views_autodelete = bool(self.executor.allow_views_autodelete)
        async with self.session_factory() as session:
            if forward_requested:
                transfer = await CanonicalPublicationLinkedForwardAtomicHandoffService(
                    session
                ).claim_linked_forward(
                    safe_publication_id,
                    holder=str(self.executor.holder),
                    ttl_seconds=int(self.executor.lease_seconds),
                    allow_time_autodelete=allow_time_autodelete,
                    allow_views_autodelete=allow_views_autodelete,
                )
            else:
                transfer = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    safe_publication_id,
                    holder=str(self.executor.holder),
                    ttl_seconds=int(self.executor.lease_seconds),
                    allow_time_autodelete=allow_time_autodelete,
                    allow_views_autodelete=allow_views_autodelete,
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
