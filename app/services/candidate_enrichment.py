from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.domain.sources.models import ContentCandidate, SourceDocument


_MAX_ENRICHMENT_INPUT_CHARS = 12_000
_MAX_SUMMARY_CHARS = 2_000
_MAX_TOPIC_CHARS = 255


class CandidateEnrichmentError(RuntimeError):
    pass


class CandidateEnrichmentBusy(CandidateEnrichmentError):
    pass


@dataclass(frozen=True, slots=True)
class EnrichmentInput:
    candidate_id: int
    source_document_id: int
    title: str | None
    source_url: str | None
    text: str
    suggested_action: str | None
    reuse_policy: str


@dataclass(frozen=True, slots=True)
class EnrichmentOutput:
    summary: str
    topic: str | None = None
    score: float | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CandidateEnrichmentResult:
    candidate: ContentCandidate
    run: CandidateEnrichmentRun
    reused_existing: bool


class CandidateEnrichmentProvider(Protocol):
    name: str
    model: str

    async def enrich(self, payload: EnrichmentInput) -> EnrichmentOutput: ...


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _bounded(value: str, limit: int) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _input_hash(document: SourceDocument, candidate: ContentCandidate) -> str:
    material = "\0".join(
        [
            str(document.content_hash or ""),
            str(document.title or ""),
            str(document.source_url or ""),
            str(candidate.suggested_action or ""),
            str((candidate.meta or {}).get("reuse_policy") or "reference_only"),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_output(output: EnrichmentOutput) -> EnrichmentOutput:
    summary = _bounded(_normalize_space(output.summary), _MAX_SUMMARY_CHARS)
    if not summary:
        raise CandidateEnrichmentError("enrichment provider returned an empty summary")
    topic = _bounded(_normalize_space(output.topic or ""), _MAX_TOPIC_CHARS) or None
    score = output.score
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError) as exc:
            raise CandidateEnrichmentError("enrichment score must be numeric") from exc
        if not 0.0 <= score <= 1.0:
            raise CandidateEnrichmentError("enrichment score must be between 0 and 1")
    return EnrichmentOutput(
        summary=summary,
        topic=topic,
        score=score,
        metadata=dict(output.metadata or {}),
    )


class LocalCandidateEnricher:
    """Deterministic offline baseline used before/when an LLM provider is unavailable."""

    name = "local"
    model = "heuristic-v1"

    async def enrich(self, payload: EnrichmentInput) -> EnrichmentOutput:
        text = _normalize_space(payload.text)
        sentences = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+", text)
            if part.strip()
        ]
        if sentences:
            summary = " ".join(sentences[:2])
        else:
            summary = text
        summary = _bounded(summary, 650)

        topic = _normalize_space(payload.title or "")
        if not topic:
            topic = _bounded(sentences[0] if sentences else text, 96)
        topic = topic or None

        length = len(text)
        score = 0.35
        if length >= 200:
            score += 0.15
        if length >= 600:
            score += 0.15
        if payload.source_url:
            score += 0.10
        if payload.title:
            score += 0.10
        score = min(score, 0.85)

        return EnrichmentOutput(
            summary=summary,
            topic=topic,
            score=score,
            metadata={
                "score_kind": "local_content_quality",
                "input_chars": len(payload.text),
            },
        )


class CandidateEnrichmentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _load(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        for_update: bool = False,
        refresh: bool = False,
    ) -> tuple[ContentCandidate, SourceDocument]:
        stmt = (
            select(ContentCandidate, SourceDocument)
            .join(
                SourceDocument,
                SourceDocument.id == ContentCandidate.source_document_id,
            )
            .where(
                ContentCandidate.id == int(candidate_id),
                ContentCandidate.channel_id == int(channel_id),
                SourceDocument.channel_id == int(channel_id),
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        if refresh:
            stmt = stmt.execution_options(populate_existing=True)
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            raise CandidateEnrichmentError("candidate not found")
        candidate, document = row
        return candidate, document

    async def _apply_completed(
        self,
        *,
        candidate: ContentCandidate,
        run: CandidateEnrichmentRun,
        provider_name: str,
        model_name: str,
    ) -> CandidateEnrichmentResult:
        candidate.summary = run.summary
        candidate.topic = run.topic
        candidate.score = run.score
        candidate.meta = {
            **dict(candidate.meta or {}),
            "enrichment_run_id": int(run.id),
            "enrichment_provider": provider_name,
            "enrichment_model": model_name,
        }
        await self.session.commit()
        await self.session.refresh(candidate)
        return CandidateEnrichmentResult(candidate, run, True)

    async def enrich(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        provider: CandidateEnrichmentProvider,
    ) -> CandidateEnrichmentResult:
        provider_name = _bounded(str(provider.name), 64)
        model_name = _bounded(str(provider.model), 191)
        if not provider_name or not model_name:
            raise CandidateEnrichmentError("enrichment provider identity is required")

        # Serialize the decision to reuse/create a run for this candidate. The
        # transaction is committed before the provider call, so no DB lock is
        # held while external work is running.
        candidate, document = await self._load(
            channel_id=channel_id,
            candidate_id=candidate_id,
            for_update=True,
            refresh=True,
        )
        if str(candidate.status or "new") != "new":
            raise CandidateEnrichmentError("candidate is no longer active")

        input_hash = _input_hash(document, candidate)
        completed = (
            await self.session.execute(
                select(CandidateEnrichmentRun)
                .where(
                    CandidateEnrichmentRun.candidate_id == int(candidate.id),
                    CandidateEnrichmentRun.provider == provider_name,
                    CandidateEnrichmentRun.model == model_name,
                    CandidateEnrichmentRun.input_hash == input_hash,
                    CandidateEnrichmentRun.status == "completed",
                )
                .order_by(CandidateEnrichmentRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if completed is not None:
            return await self._apply_completed(
                candidate=candidate,
                run=completed,
                provider_name=provider_name,
                model_name=model_name,
            )

        running = (
            await self.session.execute(
                select(CandidateEnrichmentRun.id).where(
                    CandidateEnrichmentRun.candidate_id == int(candidate.id),
                    CandidateEnrichmentRun.provider == provider_name,
                    CandidateEnrichmentRun.model == model_name,
                    CandidateEnrichmentRun.input_hash == input_hash,
                    CandidateEnrichmentRun.status == "running",
                )
            )
        ).scalar_one_or_none()
        if running is not None:
            await self.session.rollback()
            raise CandidateEnrichmentBusy("candidate enrichment is already running")

        text = _bounded(str(document.content or ""), _MAX_ENRICHMENT_INPUT_CHARS)
        payload = EnrichmentInput(
            candidate_id=int(candidate.id),
            source_document_id=int(document.id),
            title=document.title,
            source_url=document.source_url,
            text=text,
            suggested_action=candidate.suggested_action,
            reuse_policy=str(
                (candidate.meta or {}).get("reuse_policy")
                or (document.meta or {}).get("reuse_policy")
                or "reference_only"
            ),
        )
        run = CandidateEnrichmentRun(
            candidate_id=int(candidate.id),
            provider=provider_name,
            model=model_name,
            status="running",
            input_hash=input_hash,
            input_chars=len(text),
            output={},
        )
        self.session.add(run)
        try:
            await self.session.commit()
            await self.session.refresh(run)
        except Exception:
            await self.session.rollback()
            raise

        try:
            output = _validate_output(await provider.enrich(payload))
        except Exception as exc:
            run = await self.session.get(CandidateEnrichmentRun, int(run.id))
            if run is not None:
                run.status = "failed"
                run.error = type(exc).__name__
                run.finished_at = datetime.now(timezone.utc)
                await self.session.commit()
            if isinstance(exc, CandidateEnrichmentError):
                raise
            raise CandidateEnrichmentError("candidate enrichment failed") from exc

        run = await self.session.get(CandidateEnrichmentRun, int(run.id))
        if run is None:
            raise CandidateEnrichmentError("enrichment run disappeared")
        candidate, document = await self._load(
            channel_id=channel_id,
            candidate_id=candidate_id,
            for_update=True,
            refresh=True,
        )

        run.summary = output.summary
        run.topic = output.topic
        run.score = output.score
        run.error = None
        run.finished_at = datetime.now(timezone.utc)
        output_meta = dict(output.metadata or {})

        if str(candidate.status or "new") != "new":
            run.status = "discarded"
            run.output = {**output_meta, "discard_reason": "candidate_not_active"}
            await self.session.commit()
            raise CandidateEnrichmentError("candidate is no longer active")

        if _input_hash(document, candidate) != input_hash:
            run.status = "stale"
            run.output = {**output_meta, "discard_reason": "source_snapshot_changed"}
            await self.session.commit()
            raise CandidateEnrichmentError("source changed during enrichment")

        run.status = "completed"
        run.output = output_meta
        candidate.summary = output.summary
        candidate.topic = output.topic
        candidate.score = output.score
        candidate.meta = {
            **dict(candidate.meta or {}),
            "enrichment_run_id": int(run.id),
            "enrichment_provider": provider_name,
            "enrichment_model": model_name,
        }
        try:
            await self.session.commit()
            await self.session.refresh(run)
            await self.session.refresh(candidate)
            return CandidateEnrichmentResult(candidate, run, False)
        except Exception:
            await self.session.rollback()
            raise
