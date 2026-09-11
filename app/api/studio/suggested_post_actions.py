from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.api.studio.sources import CandidateResponse, _candidate_response
from app.bot.bot_instance import bot
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo
from app.services.suggested_post_actions import (
    SuggestedPostAction,
    SuggestedPostActionError,
    SuggestedPostActionFailure,
    SuggestedPostActionService,
)


router = APIRouter(prefix="/api/studio", tags=["inbox", "telegram"])


class SuggestedPostActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "decline"]
    comment: str | None = Field(default=None, max_length=128)


async def _session_dependency() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_dependency)]
PrincipalDep = Annotated[StudioPrincipal, Depends(require_studio_principal)]


_HTTP_ERRORS: dict[SuggestedPostActionFailure, tuple[int, str]] = {
    SuggestedPostActionFailure.CANDIDATE_NOT_FOUND: (404, "Candidate not found"),
    SuggestedPostActionFailure.MALFORMED_PROVENANCE: (
        409,
        "Suggested Post provenance is unavailable",
    ),
    SuggestedPostActionFailure.ROUTING_MISMATCH: (
        409,
        "Suggested Post routing no longer matches this channel",
    ),
    SuggestedPostActionFailure.INSUFFICIENT_RIGHTS: (
        403,
        "Bot no longer has the required Telegram permission",
    ),
    SuggestedPostActionFailure.NOT_ACTIONABLE: (
        409,
        "Suggested Post is no longer actionable",
    ),
    SuggestedPostActionFailure.NATIVE_MISSING: (
        410,
        "Suggested Post is no longer available in Telegram",
    ),
    SuggestedPostActionFailure.TRANSIENT_FAILURE: (
        503,
        "Telegram is temporarily unavailable; try again",
    ),
    SuggestedPostActionFailure.INVALID_REQUEST: (
        422,
        "Suggested Post action parameters are invalid",
    ),
}


@router.post(
    "/candidates/{candidate_id}/suggested-post-action",
    response_model=CandidateResponse,
)
async def mutate_suggested_post(
    candidate_id: int,
    request: SuggestedPostActionRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> CandidateResponse:
    client = await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )
    try:
        result = await SuggestedPostActionService(session, bot=bot).execute(
            candidate_id=candidate_id,
            actor_client_id=int(client.id),
            action=SuggestedPostAction(request.action),
            comment=request.comment,
        )
    except SuggestedPostActionError as exc:
        status_code, detail = _HTTP_ERRORS[exc.failure]
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return _candidate_response(result.candidate, result.document)
