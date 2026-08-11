from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.domain.publishing.models import Publication
from app.services.canonical_publication_legacy_transport_handoff import (
    CanonicalPublicationLegacyTransportHandoffService,
)


class CanonicalPublicationDeliveryExecutorLike(Protocol):
    async def execute(self, publication_id: int): ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryHandoffExecutionResult:
    outcome: str
    handoff_outcome: str


class CanonicalPublicationDeliveryHandoffExecutor:
    """Authorize linked transport retirement before invoking canonical delivery.

    The wrapper never calls the provider itself. For already canonical-only rows it
    delegates directly to the existing exact-token executor. For linked rows it first
    commits the atomic legacy transport handoff in a short DB transaction; only a
    successful `retired` outcome permits the provider-capable executor to run.

    A crash after handoff commit but before delegate claim is safe: the Publication is
    still `queued` and transport-free, so a later worker tick can claim it canonically.
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

        if linked:
            async with self.session_factory() as session:
                handoff = await CanonicalPublicationLegacyTransportHandoffService(
                    session
                ).retire_for_canonical_delivery(safe_publication_id)
            if handoff.outcome != "retired":
                return CanonicalPublicationDeliveryHandoffExecutionResult(
                    outcome="ineligible",
                    handoff_outcome=str(handoff.outcome),
                )

        return await self.executor.execute(safe_publication_id)
