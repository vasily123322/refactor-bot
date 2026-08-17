from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.bot.bot_instance import bot as tg_bot
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo
from app.services.studio_channel_onboarding_requests import (
    AiogramPreparedChannelButtonProvider,
    ChannelOnboardingPrepareError,
    ChannelOnboardingRequestView,
    StudioChannelOnboardingRequestService,
)


router = APIRouter(prefix="/api/studio/channel-onboarding", tags=["channel-onboarding"])


async def _session_dependency() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_dependency)]
PrincipalDep = Annotated[StudioPrincipal, Depends(require_studio_principal)]

_request_service = StudioChannelOnboardingRequestService(
    session_factory=AsyncSessionLocal,
    provider=AiogramPreparedChannelButtonProvider(tg_bot),
)


class ChannelOnboardingPrepareResponse(BaseModel):
    request_id: int
    prepared_button_id: str
    status: str
    expires_at: datetime


class ChannelOnboardingStatusResponse(BaseModel):
    request_id: int
    status: str
    expires_at: datetime
    channel_id: int | None
    failure_reason: str | None


def _status_response(view: ChannelOnboardingRequestView) -> ChannelOnboardingStatusResponse:
    return ChannelOnboardingStatusResponse(
        request_id=view.request_id,
        status=view.status,
        expires_at=view.expires_at,
        channel_id=view.channel_id,
        failure_reason=view.failure_reason,
    )


async def _client_id(session: AsyncSession, principal: StudioPrincipal) -> int:
    client = await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )
    return int(client.id)


@router.post("/prepare", response_model=ChannelOnboardingPrepareResponse)
async def prepare_channel_onboarding(
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelOnboardingPrepareResponse:
    client_id = await _client_id(session, principal)
    try:
        prepared = await _request_service.prepare(
            client_id=client_id,
            tg_user_id=principal.tg_user_id,
        )
    except ChannelOnboardingPrepareError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Telegram could not prepare channel selection",
        ) from exc
    return ChannelOnboardingPrepareResponse(
        request_id=prepared.request_id,
        prepared_button_id=prepared.prepared_button_id,
        status=prepared.status,
        expires_at=prepared.expires_at,
    )


@router.get("/{request_id}", response_model=ChannelOnboardingStatusResponse)
async def get_channel_onboarding_status(
    request_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelOnboardingStatusResponse:
    client_id = await _client_id(session, principal)
    view = await _request_service.get_for_client(
        client_id=client_id,
        request_id=request_id,
    )
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return _status_response(view)


@router.post("/{request_id}/cancel", response_model=ChannelOnboardingStatusResponse)
async def cancel_channel_onboarding(
    request_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> ChannelOnboardingStatusResponse:
    client_id = await _client_id(session, principal)
    view = await _request_service.cancel_for_client(
        client_id=client_id,
        request_id=request_id,
    )
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return _status_response(view)
