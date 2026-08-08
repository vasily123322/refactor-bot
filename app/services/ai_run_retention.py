from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.domain.sources.models import ContentCandidate
from app.domain.sources.rewrite import CandidateRewriteRun


TERMINAL_AI_RUN_STATUSES = frozenset(
    {"completed", "failed", "stale", "discarded", "abandoned"}
)
DEFAULT_RETENTION_DAYS = 90
DEFAULT_KEEP_RECENT = 5
DEFAULT_CANDIDATE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class AIRunRetentionResult:
    candidates_scanned: int
    enrichment_deleted: int
    rewrite_deleted: int

    @property
    def total_deleted(self) -> int:
        return self.enrichment_deleted + self.rewrite_deleted


def _meta_run_id(candidate: ContentCandidate, key: str) -> int | None:
    raw = dict(candidate.meta or {}).get(key)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class AIRunRetentionService:
    """Bound old terminal provenance without touching active/current AI runs.

    Safety contract:
    - active Inbox candidates (`status=new`) are never cleaned;
    - `running` rows are never eligible;
    - current run IDs referenced by candidate metadata are always preserved;
    - the newest N terminal rows of each kind are always preserved;
    - only rows older than the retention horizon are deleted.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _protected_ids(
        self,
        *,
        model,
        candidate_id: int,
        current_run_id: int | None,
        keep_recent: int,
    ) -> set[int]:
        newest = list(
            (
                await self.session.execute(
                    select(model.id)
                    .where(
                        model.candidate_id == int(candidate_id),
                        model.status.in_(TERMINAL_AI_RUN_STATUSES),
                    )
                    .order_by(model.id.desc())
                    .limit(max(0, int(keep_recent)))
                )
            ).scalars().all()
        )
        protected = {int(value) for value in newest}
        if current_run_id is not None:
            protected.add(int(current_run_id))
        return protected

    async def _delete_old_terminal(
        self,
        *,
        model,
        candidate_id: int,
        current_run_id: int | None,
        cutoff: datetime,
        keep_recent: int,
    ) -> int:
        protected = await self._protected_ids(
            model=model,
            candidate_id=candidate_id,
            current_run_id=current_run_id,
            keep_recent=keep_recent,
        )
        stmt = delete(model).where(
            model.candidate_id == int(candidate_id),
            model.status.in_(TERMINAL_AI_RUN_STATUSES),
            model.created_at < cutoff,
        )
        if protected:
            stmt = stmt.where(model.id.not_in(protected))
        result = await self.session.execute(stmt)
        return max(0, int(result.rowcount or 0))

    async def cleanup(
        self,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        now: datetime | None = None,
    ) -> AIRunRetentionResult:
        retention_days = max(1, int(retention_days))
        keep_recent = max(1, min(int(keep_recent), 100))
        candidate_limit = max(1, min(int(candidate_limit), 1000))
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(days=retention_days)

        candidates = list(
            (
                await self.session.execute(
                    select(ContentCandidate)
                    .where(ContentCandidate.status != "new")
                    .order_by(ContentCandidate.id.asc())
                    .limit(candidate_limit)
                )
            ).scalars().all()
        )

        enrichment_deleted = 0
        rewrite_deleted = 0
        try:
            for candidate in candidates:
                enrichment_deleted += await self._delete_old_terminal(
                    model=CandidateEnrichmentRun,
                    candidate_id=int(candidate.id),
                    current_run_id=_meta_run_id(candidate, "enrichment_run_id"),
                    cutoff=cutoff,
                    keep_recent=keep_recent,
                )
                rewrite_deleted += await self._delete_old_terminal(
                    model=CandidateRewriteRun,
                    candidate_id=int(candidate.id),
                    current_run_id=_meta_run_id(candidate, "rewrite_run_id"),
                    cutoff=cutoff,
                    keep_recent=keep_recent,
                )
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        return AIRunRetentionResult(
            candidates_scanned=len(candidates),
            enrichment_deleted=enrichment_deleted,
            rewrite_deleted=rewrite_deleted,
        )
