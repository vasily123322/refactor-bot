from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.core.db import AsyncSessionLocal
from app.domain.content.models import MediaAsset
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import MediaAssetsRepo


router = APIRouter(prefix="/api/studio", tags=["media-assets"])

_MEDIA_KINDS = ("photo", "video", "animation", "audio", "voice_note")


class MediaAssetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["photo", "video", "animation", "audio", "voice_note"]
    telegram_file_id: str | None = Field(default=None, min_length=1, max_length=1024)
    storage_url: str | None = Field(default=None, min_length=1, max_length=2048)
    label: str | None = Field(default=None, max_length=120)
    mime_type: str | None = Field(default=None, max_length=255)
    width: int | None = Field(default=None, ge=1, le=10000)
    height: int | None = Field(default=None, ge=1, le=10000)
    duration_seconds: int | None = Field(default=None, ge=0, le=86400)
    size_bytes: int | None = Field(default=None, ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def validate_transport(self):
        file_id = (self.telegram_file_id or "").strip()
        storage_url = (self.storage_url or "").strip()
        if bool(file_id) == bool(storage_url):
            raise ValueError("provide exactly one of telegram_file_id or storage_url")
        if file_id:
            if any(character.isspace() for character in file_id):
                raise ValueError("telegram_file_id must not contain whitespace")
            self.telegram_file_id = file_id
        if storage_url:
            parsed = urlsplit(storage_url)
            if parsed.scheme.lower() != "https" or not parsed.hostname:
                raise ValueError("storage_url must be an absolute HTTPS URL")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("storage_url must not contain embedded credentials")
            self.storage_url = storage_url
        if self.label is not None:
            self.label = self.label.strip() or None
        return self


class MediaAssetResponse(BaseModel):
    id: int
    channel_id: int
    kind: str
    source: str
    transport: Literal["telegram", "https"]
    label: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    duration_seconds: int | None
    size_bytes: int | None
    created_at: datetime | None


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


def _response(asset: MediaAsset) -> MediaAssetResponse:
    metadata = dict(asset.meta or {})
    return MediaAssetResponse(
        id=int(asset.id),
        channel_id=int(asset.channel_id),
        kind=str(asset.kind),
        source=str(asset.source),
        transport="telegram" if asset.telegram_file_id else "https",
        label=(str(metadata.get("label")) if metadata.get("label") else None),
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
        duration_seconds=asset.duration_seconds,
        size_bytes=asset.size_bytes,
        created_at=asset.created_at,
    )


@router.get(
    "/channels/{channel_id}/media-assets",
    response_model=list[MediaAssetResponse],
)
async def list_media_assets(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    limit: int = 100,
) -> list[MediaAssetResponse]:
    await _require_owned_channel(session, principal, channel_id)
    rows = await MediaAssetsRepo(session).list_by_channel(
        channel_id,
        limit=max(1, min(int(limit), 200)),
    )
    return [_response(row) for row in rows]


@router.post(
    "/channels/{channel_id}/media-assets",
    response_model=MediaAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_media_asset(
    channel_id: int,
    request: MediaAssetCreateRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> MediaAssetResponse:
    await _require_owned_channel(session, principal, channel_id)
    source = "studio_telegram" if request.telegram_file_id else "studio_https"
    asset = await MediaAssetsRepo(session).create(
        channel_id=channel_id,
        kind=request.kind,
        source=source,
        telegram_file_id=request.telegram_file_id,
        storage_url=request.storage_url,
        mime_type=request.mime_type,
        width=request.width,
        height=request.height,
        duration_seconds=request.duration_seconds,
        size_bytes=request.size_bytes,
        metadata={"label": request.label} if request.label else {},
    )
    return _response(asset)
