from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
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
from app.services.channel_dm_reply_intents import (
    ChannelDMReplyIntentError,
    ChannelDMReplyIntentFailure,
    ChannelDMReplyIntentList,
    ChannelDMReplyIntentService,
    ChannelDMReplyIntentView,
)
from app.services.channel_dm_reply_lifecycle import (
    ChannelDMReplyLifecycleError,
    ChannelDMReplyLifecycleFailure,
    ChannelDMReplyLifecycleReader,
    ChannelDMReplyLifecycleResult,
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


class ChannelDMReplyLifecycleCommandResponse(BaseModel):
    command_id: int
    reply_text: str
    state: Literal["pending", "sent", "failed", "uncertain"]
    requested_at: datetime
    sent_at: datetime | None = None
    finished_at: datetime | None = None
    error_class: str | None = None


class ChannelDMReplyLifecycleResponse(BaseModel):
    candidate_id: int
    commands: list[ChannelDMReplyLifecycleCommandResponse]


class ChannelDMReplyLifecycleBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[int] = Field(min_length=1, max_length=100)


class ChannelDMReplyLifecycleBatchResponse(BaseModel):
    lifecycles: list[ChannelDMReplyLifecycleResponse]


class ChannelDMReplyProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelDMReplyProposalResponse(BaseModel):
    candidate_id: int
    reply_text: str


class ChannelDMReplyIntentManualRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_text: str = Field(min_length=1, max_length=4096)


class ChannelDMReplyIntentEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_text: str = Field(min_length=1, max_length=4096)


class ChannelDMReplyIntentEmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelDMReplyIntentBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[int] = Field(min_length=1, max_length=100)


class ChannelDMReplyIntentResponse(BaseModel):
    intent_id: int
    candidate_id: int
    reply_text: str
    origin: Literal["manual", "ai", "automation"]
    state: Literal["pending_review", "dismissed", "stale", "consumed"]
    is_current: bool
    handoff_in_progress: bool
    consumed_command_id: int | None = None
    created_at: datetime
    updated_at: datetime
    consumed_at: datetime | None = None
    dismissed_at: datetime | None = None
    stale_at: datetime | None = None


class ChannelDMReplyIntentListResponse(BaseModel):
    candidate_id: int
    intents: list[ChannelDMReplyIntentResponse]


class ChannelDMReplyIntentBatchResponse(BaseModel):
    candidates: list[ChannelDMReplyIntentListResponse]


class ChannelDMReplyIntentWriteResponse(BaseModel):
    intent: ChannelDMReplyIntentResponse
    reused_existing: bool


class ChannelDMReplyIntentCommandResponse(BaseModel):
    command_id: int
    candidate_id: int
    state: Literal["pending", "sent", "failed", "uncertain"]
    error_class: str | None = None
    reused_existing: bool


class ChannelDMReplyIntentSendResponse(BaseModel):
    intent: ChannelDMReplyIntentResponse
    command: ChannelDMReplyIntentCommandResponse


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

_LIFECYCLE_HTTP_ERRORS: dict[ChannelDMReplyLifecycleFailure, tuple[int, str]] = {
    ChannelDMReplyLifecycleFailure.CANDIDATE_NOT_FOUND: (404, "Candidate not found"),
    ChannelDMReplyLifecycleFailure.ROUTING_MISMATCH: (409, "Channel DM reply lifecycle is unavailable"),
    ChannelDMReplyLifecycleFailure.MALFORMED_LIFECYCLE: (409, "Channel DM reply lifecycle is unavailable"),
    ChannelDMReplyLifecycleFailure.INVALID_REQUEST: (422, "Channel DM reply lifecycle request is invalid"),
}

_PROPOSAL_HTTP_ERRORS: dict[str, tuple[int, str]] = {
    ChannelDMReplyProposalFailure.CANDIDATE_NOT_FOUND: (404, "Candidate not found"),
    ChannelDMReplyProposalFailure.ROUTING_MISMATCH: (409, "Channel DM routing no longer matches this candidate"),
    ChannelDMReplyProposalFailure.MALFORMED_PROVENANCE: (409, "Channel DM provenance is unavailable"),
    ChannelDMReplyProposalFailure.AI_UNAVAILABLE: (422, "AI reply proposal is unavailable"),
    ChannelDMReplyProposalFailure.INVALID_OUTPUT: (422, "AI reply proposal is invalid"),
}

_INTENT_HTTP_ERRORS: dict[ChannelDMReplyIntentFailure, tuple[int, str]] = {
    ChannelDMReplyIntentFailure.CANDIDATE_NOT_FOUND: (404, "Candidate not found"),
    ChannelDMReplyIntentFailure.INTENT_NOT_FOUND: (404, "Channel DM reply intent not found"),
    ChannelDMReplyIntentFailure.INVALID_REQUEST: (422, "Channel DM reply intent request is invalid"),
    ChannelDMReplyIntentFailure.PROPOSAL_UNAVAILABLE: (422, "AI reply intent proposal is unavailable"),
    ChannelDMReplyIntentFailure.ROUTING_MISMATCH: (409, "Channel DM reply intent routing is unavailable"),
    ChannelDMReplyIntentFailure.STALE: (409, "Channel DM reply intent is stale"),
    ChannelDMReplyIntentFailure.NOT_REVIEWABLE: (409, "Channel DM reply intent is no longer reviewable"),
    ChannelDMReplyIntentFailure.HANDOFF_REJECTED: (409, "Channel DM reply command handoff was rejected"),
    ChannelDMReplyIntentFailure.MALFORMED_INTENT: (409, "Channel DM reply intent is unavailable"),
}


def _lifecycle_response(result: ChannelDMReplyLifecycleResult) -> ChannelDMReplyLifecycleResponse:
    return ChannelDMReplyLifecycleResponse(
        candidate_id=result.candidate_id,
        commands=[
            ChannelDMReplyLifecycleCommandResponse(
                command_id=command.command_id,
                reply_text=command.reply_text,
                state=command.state,
                requested_at=command.requested_at,
                sent_at=command.sent_at,
                finished_at=command.finished_at,
                error_class=command.error_class,
            )
            for command in result.commands
        ],
    )


def _intent_response(intent: ChannelDMReplyIntentView) -> ChannelDMReplyIntentResponse:
    return ChannelDMReplyIntentResponse(
        intent_id=intent.intent_id,
        candidate_id=intent.candidate_id,
        reply_text=intent.reply_text,
        origin=intent.origin,
        state=intent.state,
        is_current=intent.is_current,
        handoff_in_progress=intent.handoff_in_progress,
        consumed_command_id=intent.consumed_command_id,
        created_at=intent.created_at,
        updated_at=intent.updated_at,
        consumed_at=intent.consumed_at,
        dismissed_at=intent.dismissed_at,
        stale_at=intent.stale_at,
    )


def _intent_list_response(result: ChannelDMReplyIntentList) -> ChannelDMReplyIntentListResponse:
    return ChannelDMReplyIntentListResponse(
        candidate_id=result.candidate_id,
        intents=[_intent_response(intent) for intent in result.intents],
    )


async def _studio_client(session: AsyncSession, principal: StudioPrincipal):
    return await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )


def _raise_intent_http(exc: ChannelDMReplyIntentError) -> None:
    status_code, detail = _INTENT_HTTP_ERRORS[exc.failure]
    raise HTTPException(status_code=status_code, detail=detail) from exc


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
    client = await _studio_client(session, principal)
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


@router.get(
    "/candidates/{candidate_id}/channel-dm-reply-lifecycle",
    response_model=ChannelDMReplyLifecycleResponse,
)
async def read_channel_dm_reply_lifecycle(
    candidate_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyLifecycleResponse:
    client = await _studio_client(session, principal)
    try:
        result = await ChannelDMReplyLifecycleReader(session).read(
            candidate_id=candidate_id,
            actor_client_id=int(client.id),
        )
    except ChannelDMReplyLifecycleError as exc:
        status_code, detail = _LIFECYCLE_HTTP_ERRORS[exc.failure]
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return _lifecycle_response(result)


@router.post(
    "/channel-dm-reply-lifecycle/batch",
    response_model=ChannelDMReplyLifecycleBatchResponse,
)
async def read_channel_dm_reply_lifecycle_batch(
    request: ChannelDMReplyLifecycleBatchRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyLifecycleBatchResponse:
    client = await _studio_client(session, principal)
    try:
        results = await ChannelDMReplyLifecycleReader(session).read_many(
            candidate_ids=request.candidate_ids,
            actor_client_id=int(client.id),
        )
    except ChannelDMReplyLifecycleError as exc:
        status_code, detail = _LIFECYCLE_HTTP_ERRORS[exc.failure]
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return ChannelDMReplyLifecycleBatchResponse(
        lifecycles=[_lifecycle_response(result) for result in results]
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
    client = await _studio_client(session, principal)
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


@router.get(
    "/candidates/{candidate_id}/channel-dm-reply-intents",
    response_model=ChannelDMReplyIntentListResponse,
)
async def read_channel_dm_reply_intents(
    candidate_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyIntentListResponse:
    client = await _studio_client(session, principal)
    try:
        result = await ChannelDMReplyIntentService(session, bot=bot).read(
            candidate_id=candidate_id,
            actor_client_id=int(client.id),
        )
    except ChannelDMReplyIntentError as exc:
        _raise_intent_http(exc)
    return _intent_list_response(result)


@router.post(
    "/channel-dm-reply-intents/batch",
    response_model=ChannelDMReplyIntentBatchResponse,
)
async def read_channel_dm_reply_intents_batch(
    request: ChannelDMReplyIntentBatchRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyIntentBatchResponse:
    client = await _studio_client(session, principal)
    try:
        results = await ChannelDMReplyIntentService(session, bot=bot).read_many(
            candidate_ids=request.candidate_ids,
            actor_client_id=int(client.id),
        )
    except ChannelDMReplyIntentError as exc:
        _raise_intent_http(exc)
    return ChannelDMReplyIntentBatchResponse(
        candidates=[_intent_list_response(result) for result in results]
    )


@router.post(
    "/candidates/{candidate_id}/channel-dm-reply-intents/manual",
    response_model=ChannelDMReplyIntentWriteResponse,
)
async def create_manual_channel_dm_reply_intent(
    candidate_id: int,
    request: ChannelDMReplyIntentManualRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyIntentWriteResponse:
    client = await _studio_client(session, principal)
    try:
        result = await ChannelDMReplyIntentService(session, bot=bot).create_manual(
            candidate_id=candidate_id,
            actor_client_id=int(client.id),
            reply_text=request.reply_text,
        )
    except ChannelDMReplyIntentError as exc:
        _raise_intent_http(exc)
    return ChannelDMReplyIntentWriteResponse(
        intent=_intent_response(result.intent),
        reused_existing=result.reused_existing,
    )


@router.post(
    "/candidates/{candidate_id}/channel-dm-reply-intents/ai",
    response_model=ChannelDMReplyIntentWriteResponse,
)
async def create_ai_channel_dm_reply_intent(
    candidate_id: int,
    _request: ChannelDMReplyIntentEmptyRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyIntentWriteResponse:
    client = await _studio_client(session, principal)
    try:
        result = await ChannelDMReplyIntentService(session, bot=bot).create_ai(
            candidate_id=candidate_id,
            actor_client_id=int(client.id),
        )
    except ChannelDMReplyIntentError as exc:
        _raise_intent_http(exc)
    return ChannelDMReplyIntentWriteResponse(
        intent=_intent_response(result.intent),
        reused_existing=result.reused_existing,
    )


@router.patch(
    "/channel-dm-reply-intents/{intent_id}",
    response_model=ChannelDMReplyIntentResponse,
)
async def edit_channel_dm_reply_intent(
    intent_id: int,
    request: ChannelDMReplyIntentEditRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyIntentResponse:
    client = await _studio_client(session, principal)
    try:
        result = await ChannelDMReplyIntentService(session, bot=bot).edit(
            intent_id=intent_id,
            actor_client_id=int(client.id),
            reply_text=request.reply_text,
        )
    except ChannelDMReplyIntentError as exc:
        _raise_intent_http(exc)
    return _intent_response(result)


@router.post(
    "/channel-dm-reply-intents/{intent_id}/dismiss",
    response_model=ChannelDMReplyIntentResponse,
)
async def dismiss_channel_dm_reply_intent(
    intent_id: int,
    _request: ChannelDMReplyIntentEmptyRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyIntentResponse:
    client = await _studio_client(session, principal)
    try:
        result = await ChannelDMReplyIntentService(session, bot=bot).dismiss(
            intent_id=intent_id,
            actor_client_id=int(client.id),
        )
    except ChannelDMReplyIntentError as exc:
        _raise_intent_http(exc)
    return _intent_response(result)


@router.post(
    "/channel-dm-reply-intents/{intent_id}/send",
    response_model=ChannelDMReplyIntentSendResponse,
)
async def send_channel_dm_reply_intent(
    intent_id: int,
    _request: ChannelDMReplyIntentEmptyRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelDMReplyIntentSendResponse:
    client = await _studio_client(session, principal)
    try:
        result = await ChannelDMReplyIntentService(session, bot=bot).send(
            intent_id=intent_id,
            actor_client_id=int(client.id),
        )
    except ChannelDMReplyIntentError as exc:
        _raise_intent_http(exc)
    return ChannelDMReplyIntentSendResponse(
        intent=_intent_response(result.intent),
        command=ChannelDMReplyIntentCommandResponse(
            command_id=result.command.command_id,
            candidate_id=result.command.candidate_id,
            state=result.command.state,
            error_class=result.command.error_class,
            reused_existing=result.command.reused_existing,
        ),
    )
