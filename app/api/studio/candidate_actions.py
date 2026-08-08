from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.api.studio.schemas import ContentDetailResponse
from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.candidate_drafts import CandidateDraftError, CandidateDraftService


router = APIRouter(prefix="/api/studio", tags=["inbox"])


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
