from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.api.studio.schemas import ContentDetailResponse
from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.candidate_drafts import CandidateDraftError, CandidateDraftService
from app.services.candidate_enrichment import (
    CandidateEnrichmentBusy,
    CandidateEnrichmentError,
    CandidateEnrichmentResult,
    CandidateEnrichmentService,
    LocalCandidateEnricher,
)
from app.services.candidate_enrichment_ai import ChannelAIEnrichmentProviderFactory
from app.services.candidate_enrichment_batch import LocalBatchEnrichmentService


router = APIRouter(prefix="/api/studio", tags=["inbox"])


class CandidateEnrichmentResponse(BaseModel):
    candidate_id: int
    candidate_status: str
    summary: str | None
    topic: str | None
    score: float | None
    run_id: int
    run_status: str
    provider: str
    model: str | None
    reused_existing: bool
    output: dict[str, Any]


class LocalBatchEnrichmentRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=100)


class LocalBatchEnrichmentResponse(BaseModel):
    selected: int
    completed: int
    reused: int
    skipped_busy: int
    failed: int


async def _session_dependency() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_dependency)]
PrincipalDep = Annotated[StudioPrincipal, Depends(require_studio_principal)]


async def _require_owned_channel(
    session: AsyncSession,
    principal: StudioPrincipal,
    channel_id: int,
) -> None:
    client = await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )
    channel = await ChannelsRepo(session).get_by_id(int(channel_id))
    if channel is None or int(channel.owner_id) != int(client.id):
        raise HTTPException(status_code=404, detail="Channel not found")


def _enrichment_response(result: CandidateEnrichmentResult) -> CandidateEnrichmentResponse:
    return CandidateEnrichmentResponse(
        candidate_id=int(result.candidate.id),
        candidate_status=str(result.candidate.status),
        summary=result.candidate.summary,
        topic=result.candidate.topic,
        score=result.candidate.score,
        run_id=int(result.run.id),
        run_status=str(result.run.status),
        provider=str(result.run.provider),
        model=result.run.model,
        reused_existing=bool(result.reused_existing),
        output=dict(result.run.output or {}),
    )


def _raise_enrichment_http(exc: CandidateEnrichmentError) -> None:
    if isinstance(exc, CandidateEnrichmentBusy):
        raise HTTPException(status_code=409, detail="Candidate enrichment is already running") from exc
    if str(exc) == "candidate not found":
        raise HTTPException(status_code=404, detail="Candidate not found") from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_id}/candidates/{candidate_id}/draft",
    response_model=ContentDetailResponse,
)
async def create_candidate_draft(
    channel_id: int,
    candidate_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> ContentDetailResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        result = await CandidateDraftService(session).create(
            channel_id=channel_id,
            candidate_id=candidate_id,
            created_by_tg_user_id=principal.tg_user_id,
        )
    except CandidateDraftError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    item = result.item
    return ContentDetailResponse(
        id=int(item.id),
        channel_id=int(item.channel_id),
        kind=str(item.kind),
        status=str(item.status),
        title=item.title,
        current_revision=int(item.current_revision or 0),
        updated_at=item.updated_at,
        document=result.document.to_dict(),
    )


@router.post(
    "/channels/{channel_id}/candidates/enrich/local-batch",
    response_model=LocalBatchEnrichmentResponse,
)
async def enrich_candidates_local_batch(
    channel_id: int,
    request: LocalBatchEnrichmentRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> LocalBatchEnrichmentResponse:
    await _require_owned_channel(session, principal, channel_id)
    result = await LocalBatchEnrichmentService(session).run(
        channel_id=channel_id,
        limit=request.limit,
    )
    return LocalBatchEnrichmentResponse(
        selected=result.selected,
        completed=result.completed,
        reused=result.reused,
        skipped_busy=result.skipped_busy,
        failed=result.failed,
    )


@router.post(
    "/channels/{channel_id}/candidates/{candidate_id}/enrich/local",
    response_model=CandidateEnrichmentResponse,
)
async def enrich_candidate_local(
    channel_id: int,
    candidate_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> CandidateEnrichmentResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        result = await CandidateEnrichmentService(session).enrich(
            channel_id=channel_id,
            candidate_id=candidate_id,
            provider=LocalCandidateEnricher(),
        )
    except CandidateEnrichmentError as exc:
        _raise_enrichment_http(exc)
        raise AssertionError("unreachable")
    return _enrichment_response(result)


@router.post(
    "/channels/{channel_id}/candidates/{candidate_id}/enrich/ai",
    response_model=CandidateEnrichmentResponse,
)
async def enrich_candidate_ai(
    channel_id: int,
    candidate_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> CandidateEnrichmentResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        provider = await ChannelAIEnrichmentProviderFactory(session).build(channel_id)
        result = await CandidateEnrichmentService(session).enrich(
            channel_id=channel_id,
            candidate_id=candidate_id,
            provider=provider,
        )
    except CandidateEnrichmentError as exc:
        _raise_enrichment_http(exc)
        raise AssertionError("unreachable")
    return _enrichment_response(result)
