from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.api.studio.schemas import ContentDetailResponse
from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.candidate_current_structured_rewrite import (
    CandidateCurrentStructuredRewriteError,
    CandidateCurrentStructuredRewriteService,
    CurrentCandidateStructuredRewrite,
)


router = APIRouter(prefix="/api/studio", tags=["inbox", "ai"])


class CurrentStructuredRewriteResponse(BaseModel):
    candidate_id: int
    run_id: int
    provider: str
    model: str | None
    text: str
    document: dict


class ApplyCurrentStructuredRewriteRequest(BaseModel):
    run_id: int = Field(ge=1)


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


def _response(current: CurrentCandidateStructuredRewrite) -> CurrentStructuredRewriteResponse:
    return CurrentStructuredRewriteResponse(
        candidate_id=int(current.candidate.id),
        run_id=int(current.run.id),
        provider=str(current.run.provider),
        model=current.run.model,
        text=str(current.run.text or ""),
        document=current.document.to_dict(),
    )


@router.get(
    "/channels/{channel_id}/candidates/{candidate_id}/rewrite/ai/structured/current",
    response_model=CurrentStructuredRewriteResponse | None,
)
async def current_structured_rewrite(
    channel_id: int,
    candidate_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> CurrentStructuredRewriteResponse | None:
    await _require_owned_channel(session, principal, channel_id)
    current = await CandidateCurrentStructuredRewriteService(session).current(
        channel_id=channel_id,
        candidate_id=candidate_id,
    )
    return _response(current) if current is not None else None


@router.post(
    "/channels/{channel_id}/candidates/{candidate_id}/rewrite/ai/structured/apply",
    response_model=ContentDetailResponse,
)
async def apply_current_structured_rewrite(
    channel_id: int,
    candidate_id: int,
    request: ApplyCurrentStructuredRewriteRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ContentDetailResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        result = await CandidateCurrentStructuredRewriteService(session).apply(
            channel_id=channel_id,
            candidate_id=candidate_id,
            expected_run_id=request.run_id,
            created_by_tg_user_id=principal.tg_user_id,
        )
    except CandidateCurrentStructuredRewriteError as exc:
        if str(exc) == "candidate not found":
            raise HTTPException(status_code=404, detail="Candidate not found") from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
