from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.domain.publishing.models import Publication
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CanonicalPublicationAtomicHandoffClaimService,
)
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
)


class CanonicalPublicationDeliveryExecutorLike(Protocol):
    holder: str
    lease_seconds: int
    allow_time_autodelete: bool

    async def execute(self, publication_id: int): ...

    async def execute_claim(
        self,
        claim: CanonicalPublicationDeliveryClaim,
    ): ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryHandoffExecutionResult:
    outcome: str
    handoff_outcome: str


class CanonicalPublicationDeliveryHandoffExecutor:
    """Execute canonical-only rows directly and linked rows via atomic authority transfer.

    For a linked row, legacy transport retirement and the canonical `sending + attempt +
    delivery lease` claim now share one database transaction. There is no committed
    transport-free `queued` window between handoff and claim.

    Only the resulting committed exact claim is passed to provider execution. Any
    pre-claim rejection restores the pending PostTask; any post-commit claim-unavailable
    result is treated as lease-lost/recovery-owned and never causes a second claim/send.
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

        if not linked:
            return await self.executor.execute(safe_publication_id)

        async with self.session_factory() as session:
            transfer = await CanonicalPublicationAtomicHandoffClaimService(
                session
            ).claim_linked(
                safe_publication_id,
                holder=str(self.executor.holder),
                ttl_seconds=int(self.executor.lease_seconds),
                allow_time_autodelete=bool(self.executor.allow_time_autodelete),
            )

        if transfer.outcome == "claim_unavailable":
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
