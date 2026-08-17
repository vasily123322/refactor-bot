from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.candidate_current_structured_rewrite import (
    CandidateCurrentStructuredRewriteService,
)
from app.services.candidate_rewrite import (
    CandidateRewriteBusy,
    CandidateRewriteError,
    CandidateRewriteService,
)
from app.services.candidate_structured_edit_ai import (
    ChannelAIStructuredEditProviderFactory,
    StructuredEditOperation,
    structured_edit_input_variant,
)
from app.services.candidate_structured_rewrite_ai import structured_document_from_run_output


router = APIRouter(prefix="/api/studio", tags=["inbox", "ai"])


class StructuredEditRequest(BaseModel):
    run_id: int = Field(ge=1)
    operation: Literal["shorten", "expand", "to_list", "add_headings"]


class StructuredEditResponse(BaseModel):
    candidate_id: int
    candidate_status: str
    run_id: int
    run_status: str
    provider: str
    model: str | None
    text: str
    reused_existing: bool
    output: dict[str, Any]
    document: dict[str, Any]


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


@router.post(
    "/channels/{channel_id}/candidates/{candidate_id}/rewrite/ai/structured/edit",
    response_model=StructuredEditResponse,
)
async def edit_current_structured_rewrite(
    channel_id: int,
    candidate_id: int,
    request: StructuredEditRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> StructuredEditResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        current = await CandidateCurrentStructuredRewriteService(session).current(
            channel_id=channel_id,
            candidate_id=candidate_id,
        )
        if current is None or int(current.run.id) != int(request.run_id):
            raise CandidateRewriteError("candidate structured rewrite is no longer current")
        operation: StructuredEditOperation = request.operation
        provider = await ChannelAIStructuredEditProviderFactory(session).build(
            channel_id,
            parent_run_id=int(current.run.id),
            document=current.document,
            operation=operation,
        )
        result = await CandidateRewriteService(session).rewrite(
            channel_id=channel_id,
            candidate_id=candidate_id,
            provider=provider,
            input_variant=structured_edit_input_variant(
                parent_run_id=int(current.run.id),
                document=current.document,
                operation=operation,
            ),
            expected_current_run_id=int(current.run.id),
        )
        document = structured_document_from_run_output(result.run.output)
        if document is None:
            raise CandidateRewriteError("structured edit run has no valid PostDocument")
    except CandidateRewriteBusy as exc:
        raise HTTPException(status_code=409, detail="Candidate rewrite is already running") from exc
    except CandidateRewriteError as exc:
        if str(exc) == "candidate not found":
            raise HTTPException(status_code=404, detail="Candidate not found") from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return StructuredEditResponse(
        candidate_id=int(result.candidate.id),
        candidate_status=str(result.candidate.status),
        run_id=int(result.run.id),
        run_status=str(result.run.status),
        provider=str(result.run.provider),
        model=result.run.model,
        text=str(result.run.text or ""),
        reused_existing=bool(result.reused_existing),
        output=dict(result.run.output or {}),
        document=document.to_dict(),
    )
