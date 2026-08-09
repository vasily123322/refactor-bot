from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.api.studio.media_assets import MediaAssetResponse, _response
from app.bot.bot_instance import bot as tg_bot
from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.source_media_promotion import (
    SourceMediaPromotionError,
    SourceMediaPromotionService,
)
from app.userbot.client import app as userbot


router = APIRouter(prefix="/api/studio", tags=["candidate-media"])


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
    "/channels/{channel_id}/candidates/{candidate_id}/media-asset",
    response_model=MediaAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def promote_candidate_media(
    channel_id: int,
    candidate_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> MediaAssetResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        asset = await SourceMediaPromotionService(
            session,
            userbot_gateway=userbot,
            bot=tg_bot,
        ).promote(
            channel_id=channel_id,
            candidate_id=candidate_id,
            tg_user_id=principal.tg_user_id,
        )
    except SourceMediaPromotionError as exc:
        detail = str(exc)
        if detail in {"candidate not found", "source document not found"}:
            raise HTTPException(status_code=404, detail="Source media not found") from exc
        if detail == "source media kind is not promotable":
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=422, detail=detail) from exc
    return _response(asset)
