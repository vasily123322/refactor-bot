from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.api.studio.schemas import PlannerEntryResponse, PlannerRescheduleRequest
from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.planner import (
    PlannerConflictError,
    PlannerError,
    PlannerNotFoundError,
    PlannerService,
)


router = APIRouter(prefix="/api/studio", tags=["planner"])


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")


def _response(entry) -> PlannerEntryResponse:
    return PlannerEntryResponse(**asdict(entry))


@router.get(
    "/channels/{channel_id}/planner",
    response_model=list[PlannerEntryResponse],
)
async def list_planner(
    channel_id: int,
    start_at: Annotated[datetime, Query(alias="start")],
    end_at: Annotated[datetime, Query(alias="end")],
    principal: PrincipalDep,
    session: SessionDep,
    status_filter: str | None = None,
    limit: int = 500,
) -> list[PlannerEntryResponse]:
    await _require_owned_channel(session, principal, channel_id)
    statuses = (
        [value.strip() for value in status_filter.split(",") if value.strip()]
        if status_filter
        else None
    )
    try:
        rows = await PlannerService(session).list_entries(
            channel_id=channel_id,
            start=start_at,
            end=end_at,
            statuses=statuses,
            limit=limit,
        )
    except PlannerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return [_response(row) for row in rows]


@router.post(
    "/channels/{channel_id}/planner/{schedule_id}/reschedule",
    response_model=PlannerEntryResponse,
)
async def reschedule_planner_entry(
    channel_id: int,
    schedule_id: int,
    request: PlannerRescheduleRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> PlannerEntryResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        entry = await PlannerService(session).reschedule(
            channel_id=channel_id,
            schedule_id=schedule_id,
            scheduled_at=request.scheduled_at,
            timezone_name=request.timezone,
        )
    except PlannerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlannerConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _response(entry)


@router.post(
    "/channels/{channel_id}/planner/{schedule_id}/cancel",
    response_model=PlannerEntryResponse,
)
async def cancel_planner_entry(
    channel_id: int,
    schedule_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> PlannerEntryResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        entry = await PlannerService(session).cancel(
            channel_id=channel_id,
            schedule_id=schedule_id,
        )
    except PlannerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlannerConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _response(entry)
