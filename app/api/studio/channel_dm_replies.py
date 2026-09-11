from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.bot.bot_instance import bot
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo
from app.services.channel_dm_replies import (
    ChannelDMReplyError,
    ChannelDMReplyFailure,
    ChannelDMReplyService,
)
from app.services.channel_dm_reply_proposals import (
    ChannelDMReplyProposalError,
    ChannelDMReplyProposalFailure,
    ChannelDMReplyProposalService,
)


router = APIRouter(prefix="/api/studio", tags=["inbox", "telegram"])


class ChannelDMReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_text: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=8, max_length=160)


class ChannelDMReplyResponse(BaseModel):
    command_id: int
    candidate_id: int
    state: Literal["pending", "sent", "failed", "uncertain"]
    provider_message_id: int | None = None
    error_class: str | None = None
    reused_existing: bool


class ChannelDMReplyProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelDMReplyProposalResponse(BaseModel):
    candidate_id: int
    reply_text: str


async def _session_dependency() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_dependency)]
PrincipalDep = Annotated[StudioPrincipal, Depends(require_studio_principal)]


_HTTP_ERRORS: dict[ChannelDMReplyFailure, tuple[int, str]] = {
    ChannelDMReplyFailure.CANDIDATE_NOT_FOUND: (404, "Candidate not found"),
    ChannelDMReplyFailure.INVALID_REQUEST: (422, "Channel DM reply request is invalid"),
    ChannelDMReplyFailure.IDEMPOTENCY_CONFLICT: (409, "Idempotency key is already bound to another reply"),
    ChannelDMReplyFailure.MALFORMED_PROVENANCE: (409, "Channel DM provenance is unavailable"),
    ChannelDMReplyFailure.ROUTING_MISMATCH: (409, "Channel DM routing no longer matches this candidate"),
}

_PROPOSAL_HTTP_ERRORS: dict[str, tuple[int, str]] = {
    ChannelDMReplyProposalFailure.CANDIDATE_NOT_FOUND: (404, "Candidate not found"),
    ChannelDMReplyProposalFailure.ROUTING_MISMATCH: (409, "Channel DM routing no longer matches this candidate"),
    ChannelDMReplyProposalFailure.MALFORMED_PROVENANCE: (409, "Channel DM provenance is unavailable"),
    ChannelDMReplyProposalFailure.AI_UNAVAILABLE: (422, "AI reply proposal is unavailable"),
    ChannelDMReplyProposalFailure.INVALID_OUTPUT: (422, "AI reply proposal is invalid"),
}


@router.post(
    "/candidates/{candidate_id}/channel-dm-reply",
    response_model=ChannelDMReplyResponse,
)
async def reply_to_channel_dm(
    candidate_id: int,
    request: ChannelDMReplyRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyResponse:
    client = await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )
    try:
        result = await ChannelDMReplyService(session, bot=bot).execute(
            candidate_id=candidate_id,
            actor_client_id=int(client.id),
            reply_text=request.reply_text,
            idempotency_key=request.idempotency_key,
        )
    except ChannelDMReplyError as exc:
        status_code, detail = _HTTP_ERRORS[exc.failure]
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return ChannelDMReplyResponse(
        command_id=result.command_id,
        candidate_id=result.candidate_id,
        state=result.state,
        provider_message_id=result.provider_message_id,
        error_class=result.error_class,
        reused_existing=result.reused_existing,
    )


@router.post(
    "/candidates/{candidate_id}/channel-dm-reply-proposal",
    response_model=ChannelDMReplyProposalResponse,
)
async def propose_channel_dm_reply(
    candidate_id: int,
    _request: ChannelDMReplyProposalRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyProposalResponse:
    client = await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )
    try:
        result = await ChannelDMReplyProposalService(session).propose(
            candidate_id=candidate_id,
            actor_client_id=int(client.id),
        )
    except ChannelDMReplyProposalError as exc:
        status_code, detail = _PROPOSAL_HTTP_ERRORS[exc.failure]
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return ChannelDMReplyProposalResponse(
        candidate_id=result.candidate_id,
        reply_text=result.reply_text,
    )
