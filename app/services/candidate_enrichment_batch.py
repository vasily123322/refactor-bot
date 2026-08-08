from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.models import ContentCandidate
from app.services.candidate_enrichment import (
    CandidateEnrichmentBusy,
    CandidateEnrichmentError,
    CandidateEnrichmentService,
    LocalCandidateEnricher,
)


@dataclass(frozen=True, slots=True)
class LocalBatchEnrichmentResult:
    selected: int
    completed: int
    reused: int
    skipped_busy: int
    failed: int


class LocalBatchEnrichmentService:
    """Enrich untouched active Inbox candidates without external AI/token spend."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def run(self, *, channel_id: int, limit: int = 25) -> LocalBatchEnrichmentResult:
        bounded_limit = max(1, min(int(limit), 100))
        candidate_ids = list(
            (
                await self.session.execute(
                    select(ContentCandidate.id)
                    .where(
                        ContentCandidate.channel_id == int(channel_id),
                        ContentCandidate.status == "new",
                        # Every enrichment provider is required to produce a non-empty
                        # summary. Using this as the untouched sentinel prevents a local
                        # batch from replacing an existing AI result merely because its
                        # optional topic/score is null.
                        ContentCandidate.summary.is_(None),
                    )
                    .order_by(ContentCandidate.created_at.asc(), ContentCandidate.id.asc())
                    .limit(bounded_limit)
                )
            ).scalars().all()
        )

        completed = 0
        reused = 0
        skipped_busy = 0
        failed = 0
        service = CandidateEnrichmentService(self.session)
        provider = LocalCandidateEnricher()

        for candidate_id in candidate_ids:
            try:
                result = await service.enrich(
                    channel_id=int(channel_id),
                    candidate_id=int(candidate_id),
                    provider=provider,
                )
                if result.reused_existing:
                    reused += 1
                else:
                    completed += 1
            except CandidateEnrichmentBusy:
                skipped_busy += 1
            except CandidateEnrichmentError:
                # One malformed/stale candidate must not block the rest of the batch.
                failed += 1

        return LocalBatchEnrichmentResult(
            selected=len(candidate_ids),
            completed=completed,
            reused=reused,
            skipped_busy=skipped_busy,
            failed=failed,
        )
