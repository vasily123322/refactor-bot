from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.domain.sources.models import ContentCandidate
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.ai_settings import ChannelAISettingsRepo


@dataclass(frozen=True, slots=True)
class ChannelAIUsageSnapshot:
    configured: bool
    enabled: bool
    model: str | None
    temperature: float | None
    max_tokens: int | None
    tokens_used_day: int
    tokens_limit_day: int | None
    tokens_used_month: int
    tokens_limit_month: int | None


@dataclass(frozen=True, slots=True)
class AIActivityRun:
    kind: str
    id: int
    candidate_id: int
    status: str
    provider: str
    model: str | None
    input_chars: int
    error_type: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class AIActivitySnapshot:
    usage: ChannelAIUsageSnapshot
    enrichment_counts: dict[str, int]
    rewrite_counts: dict[str, int]
    runs: tuple[AIActivityRun, ...]


def _counts(rows) -> dict[str, int]:
    return {str(status): int(count) for status, count in rows}


def _run_sort_key(run: AIActivityRun) -> tuple[float, int]:
    moment = run.created_at or run.started_at
    try:
        timestamp = float(moment.timestamp()) if moment is not None else 0.0
    except (OverflowError, OSError, ValueError):
        timestamp = 0.0
    return timestamp, run.id


class AIActivityService:
    """Read-only channel AI usage and durable run provenance.

    This service never starts generation and never reads source document content. It
    intentionally exposes only operational metadata already safe for Studio owners.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def snapshot(self, *, channel_id: int, limit: int = 50) -> AIActivitySnapshot:
        channel_id = int(channel_id)
        bounded_limit = max(1, min(int(limit), 200))

        settings = await ChannelAISettingsRepo(self.session).get_by_channel_id(channel_id)
        usage = ChannelAIUsageSnapshot(
            configured=settings is not None,
            enabled=bool(settings.enabled) if settings is not None else False,
            model=str(settings.model) if settings is not None else None,
            temperature=(float(settings.temperature) if settings is not None else None),
            max_tokens=(int(settings.max_tokens) if settings is not None else None),
            tokens_used_day=(int(settings.tokens_used_day or 0) if settings is not None else 0),
            tokens_limit_day=(
                int(settings.tokens_limit_day)
                if settings is not None and settings.tokens_limit_day is not None
                else None
            ),
            tokens_used_month=(
                int(settings.tokens_used_month or 0) if settings is not None else 0
            ),
            tokens_limit_month=(
                int(settings.tokens_limit_month)
                if settings is not None and settings.tokens_limit_month is not None
                else None
            ),
        )

        enrichment_counts = _counts(
            (
                await self.session.execute(
                    select(CandidateEnrichmentRun.status, func.count(CandidateEnrichmentRun.id))
                    .join(
                        ContentCandidate,
                        ContentCandidate.id == CandidateEnrichmentRun.candidate_id,
                    )
                    .where(ContentCandidate.channel_id == channel_id)
                    .group_by(CandidateEnrichmentRun.status)
                )
            ).all()
        )
        rewrite_counts = _counts(
            (
                await self.session.execute(
                    select(CandidateRewriteRun.status, func.count(CandidateRewriteRun.id))
                    .join(
                        ContentCandidate,
                        ContentCandidate.id == CandidateRewriteRun.candidate_id,
                    )
                    .where(ContentCandidate.channel_id == channel_id)
                    .group_by(CandidateRewriteRun.status)
                )
            ).all()
        )

        enrichment_rows = list(
            (
                await self.session.execute(
                    select(CandidateEnrichmentRun)
                    .join(
                        ContentCandidate,
                        ContentCandidate.id == CandidateEnrichmentRun.candidate_id,
                    )
                    .where(ContentCandidate.channel_id == channel_id)
                    .order_by(CandidateEnrichmentRun.id.desc())
                    .limit(bounded_limit)
                )
            ).scalars().all()
        )
        rewrite_rows = list(
            (
                await self.session.execute(
                    select(CandidateRewriteRun)
                    .join(
                        ContentCandidate,
                        ContentCandidate.id == CandidateRewriteRun.candidate_id,
                    )
                    .where(ContentCandidate.channel_id == channel_id)
                    .order_by(CandidateRewriteRun.id.desc())
                    .limit(bounded_limit)
                )
            ).scalars().all()
        )

        runs = [
            AIActivityRun(
                kind="enrichment",
                id=int(row.id),
                candidate_id=int(row.candidate_id),
                status=str(row.status),
                provider=str(row.provider),
                model=row.model,
                input_chars=int(row.input_chars or 0),
                error_type=row.error,
                started_at=row.started_at,
                finished_at=row.finished_at,
                created_at=row.created_at,
            )
            for row in enrichment_rows
        ]
        runs.extend(
            AIActivityRun(
                kind="rewrite",
                id=int(row.id),
                candidate_id=int(row.candidate_id),
                status=str(row.status),
                provider=str(row.provider),
                model=row.model,
                input_chars=int(row.input_chars or 0),
                error_type=row.error,
                started_at=row.started_at,
                finished_at=row.finished_at,
                created_at=row.created_at,
            )
            for row in rewrite_rows
        )
        runs.sort(key=_run_sort_key, reverse=True)
        return AIActivitySnapshot(
            usage=usage,
            enrichment_counts=enrichment_counts,
            rewrite_counts=rewrite_counts,
            runs=tuple(runs[:bounded_limit]),
        )
