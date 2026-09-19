from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.core.db import AsyncSessionLocal
from app.domain.content.models import MediaAsset
from app.domain.sources.models import SourceDocument
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.sources_v2 import SourcesRepo


router = APIRouter(prefix="/api/studio", tags=["candidate-media"])

_MEDIA_KINDS = {
    "photo",
    "video",
    "animation",
    "audio",
    "voice_note",
    "video_note",
    "sticker",
    "document",
}
_PROMOTABLE_KINDS = {"photo", "video", "animation", "audio", "voice_note"}


class CandidateMediaResponse(BaseModel):
    candidate_id: int
    source_document_id: int
    kind: str
    mime_type: str | None
    size_bytes: int | None
    width: int | None
    height: int | None
    duration_seconds: int | None
    promotable: bool
    media_asset_id: int | None = None


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


def _bounded_positive_int(value: object, *, maximum: int) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed <= 0 or parsed > maximum:
        return None
    return parsed


def _safe_mime_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value.strip() if character.isprintable())[:255]
    return cleaned or None


def _candidate_media_response(
    *,
    candidate_id: int,
    document: SourceDocument,
) -> CandidateMediaResponse | None:
    raw = dict(document.meta or {}).get("telegram_media")
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in _MEDIA_KINDS:
        return None
    return CandidateMediaResponse(
        candidate_id=int(candidate_id),
        source_document_id=int(document.id),
        kind=kind,
        mime_type=_safe_mime_type(raw.get("mime_type")),
        size_bytes=_bounded_positive_int(raw.get("size_bytes"), maximum=2_147_483_647),
        width=_bounded_positive_int(raw.get("width"), maximum=100_000),
        height=_bounded_positive_int(raw.get("height"), maximum=100_000),
        duration_seconds=_bounded_positive_int(raw.get("duration_seconds"), maximum=86_400),
        promotable=kind in _PROMOTABLE_KINDS,
    )


@router.get(
    "/channels/{channel_id}/candidate-media",
    response_model=list[CandidateMediaResponse],
)
async def list_candidate_media(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    limit: int = 100,
    candidate_ids: str | None = None,
) -> list[CandidateMediaResponse]:
    await _require_owned_channel(session, principal, channel_id)
    repo = SourcesRepo(session)
    if candidate_ids is None:
        rows = await repo.list_candidate_rows(
            channel_id,
            status="new",
            limit=max(1, min(int(limit), 500)),
        )
    else:
        try:
            ids = [
                int(value.strip())
                for value in candidate_ids.split(",")
                if value.strip()
            ]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid candidate_ids") from exc
        ids = list(dict.fromkeys(ids))
        if not ids or len(ids) > 100 or any(candidate_id <= 0 for candidate_id in ids):
            raise HTTPException(
                status_code=422,
                detail="candidate_ids must contain 1..100 positive ids",
            )
        rows = await repo.list_candidate_rows_by_ids(
            channel_id,
            ids,
            status="new",
        )
    pending: list[tuple[CandidateMediaResponse, int | None]] = []
    requested_asset_ids: set[int] = set()
    for candidate, document in rows:
        media = _candidate_media_response(candidate_id=int(candidate.id), document=document)
        if media is None:
            continue
        raw_asset_id = _bounded_positive_int(
            dict(document.meta or {}).get("media_asset_id"),
            maximum=2_147_483_647,
        )
        if raw_asset_id is not None:
            requested_asset_ids.add(raw_asset_id)
        pending.append((media, raw_asset_id))

    allowed_asset_ids: set[int] = set()
    if requested_asset_ids:
        asset_rows = await session.execute(
            select(MediaAsset.id).where(
                MediaAsset.channel_id == int(channel_id),
                MediaAsset.id.in_(requested_asset_ids),
            )
        )
        allowed_asset_ids = {int(value) for value in asset_rows.scalars().all()}

    result: list[CandidateMediaResponse] = []
    for media, raw_asset_id in pending:
        if raw_asset_id is not None and raw_asset_id in allowed_asset_ids:
            media.media_asset_id = raw_asset_id
        result.append(media)
    return result
