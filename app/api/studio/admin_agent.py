from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.core.db import AsyncSessionLocal
from app.domain.admin_agent import AdminAgentEvent, AdminAgentRun
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.admin_agent import (
    DRAFT_SCENARIO_LIMITS,
    AdminAgentRunner,
    SCENARIO_ATTENTION_TODAY,
    SCENARIO_DRAFTS_TOMORROW,
)


router = APIRouter(prefix="/api/studio", tags=["assistant"])


async def _session_dependency() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_dependency)]
PrincipalDep = Annotated[StudioPrincipal, Depends(require_studio_principal)]


class AssistantRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["attention_today", "drafts_tomorrow"] = SCENARIO_ATTENTION_TODAY
    request_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class AssistantEventResponse(BaseModel):
    id: int
    sequence: int
    event_type: str
    tool_name: str | None
    payload: dict[str, Any]
    created_at: datetime | None


class AssistantRunResponse(BaseModel):
    id: int
    channel_id: int
    scenario: str
    request_id: str | None
    status: str
    model: str | None
    tokens_used: int
    result: dict[str, Any] | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime | None
    events: list[AssistantEventResponse] = Field(default_factory=list)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")


def _event_response(row: AdminAgentEvent) -> AssistantEventResponse:
    return AssistantEventResponse(
        id=int(row.id),
        sequence=int(row.sequence),
        event_type=str(row.event_type),
        tool_name=row.tool_name,
        payload=dict(row.payload or {}),
        created_at=row.created_at,
    )


async def _response(
    session: AsyncSession,
    row: AdminAgentRun,
    *,
    include_events: bool,
) -> AssistantRunResponse:
    events: list[AssistantEventResponse] = []
    if include_events:
        event_rows = list(
            (
                await session.execute(
                    select(AdminAgentEvent)
                    .where(AdminAgentEvent.run_id == int(row.id))
                    .order_by(AdminAgentEvent.sequence.asc())
                    .limit(100)
                )
            ).scalars()
        )
        events = [_event_response(event) for event in event_rows]
    return AssistantRunResponse(
        id=int(row.id),
        channel_id=int(row.channel_id),
        scenario=str(row.scenario),
        request_id=row.request_id,
        status=str(row.status),
        model=row.model,
        tokens_used=int(row.tokens_used or 0),
        result=dict(row.result) if isinstance(row.result, dict) else None,
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        events=events,
    )


@router.post(
    "/channels/{channel_id}/assistant/runs",
    response_model=AssistantRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assistant_run(
    channel_id: int,
    request: AssistantRunRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantRunResponse:
    await _require_owned_channel(session, principal, channel_id)
    if request.scenario == SCENARIO_ATTENTION_TODAY:
        run = await AdminAgentRunner(session).run_attention_today(
            channel_id=channel_id,
            owner_tg_user_id=principal.tg_user_id,
        )
    elif request.scenario == SCENARIO_DRAFTS_TOMORROW:
        if not request.request_id:
            raise HTTPException(status_code=422, detail="request_id is required for drafts_tomorrow")
        run = await AdminAgentRunner(
            session,
            limits=DRAFT_SCENARIO_LIMITS,
        ).run_drafts_tomorrow(
            channel_id=channel_id,
            owner_tg_user_id=principal.tg_user_id,
            request_id=request.request_id,
        )
    else:
        raise HTTPException(status_code=422, detail="Unsupported assistant scenario")
    return await _response(session, run, include_events=True)


@router.get(
    "/channels/{channel_id}/assistant/runs",
    response_model=list[AssistantRunResponse],
)
async def list_assistant_runs(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[AssistantRunResponse]:
    await _require_owned_channel(session, principal, channel_id)
    rows = list(
        (
            await session.execute(
                select(AdminAgentRun)
                .where(
                    AdminAgentRun.channel_id == int(channel_id),
                    AdminAgentRun.owner_tg_user_id == int(principal.tg_user_id),
                )
                .order_by(AdminAgentRun.id.desc())
                .limit(int(limit))
            )
        ).scalars()
    )
    return [await _response(session, row, include_events=False) for row in rows]


@router.get(
    "/channels/{channel_id}/assistant/runs/{run_id}",
    response_model=AssistantRunResponse,
)
async def get_assistant_run(
    channel_id: int,
    run_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantRunResponse:
    await _require_owned_channel(session, principal, channel_id)
    result = await session.execute(
        select(AdminAgentRun).where(
            AdminAgentRun.id == int(run_id),
            AdminAgentRun.channel_id == int(channel_id),
            AdminAgentRun.owner_tg_user_id == int(principal.tg_user_id),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Assistant run not found")
    return await _response(session, row, include_events=True)
