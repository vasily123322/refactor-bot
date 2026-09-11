from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.api.studio.candidate_actions import router as candidate_actions_router
from app.api.studio.candidate_media import router as candidate_media_router
from app.api.studio.candidate_prompt_rewrite import router as candidate_prompt_rewrite_router
from app.api.studio.candidate_rewrite_authority import router as candidate_rewrite_authority_router
from app.api.studio.channel_dm_replies import router as channel_dm_replies_router
from app.api.studio.channel_onboarding import router as channel_onboarding_router
from app.api.studio.config import StudioConfig, studio_config
from app.api.studio.media_assets import router as media_assets_router
from app.api.studio.planner import router as planner_router
from app.api.studio.schemas import (
    ChannelResponse,
    ContentCreateRequest,
    ContentDetailResponse,
    ContentRevisionRequest,
    ContentSummaryResponse,
    PreviewRequest,
    PreviewResponse,
    PublicationResponse,
    ScheduleRequest,
    StudioUserResponse,
    TelegramPreviewRequest,
    TelegramPreviewResponse,
)
from app.api.studio.source_media_assets import router as source_media_assets_router
from app.api.studio.sources import router as sources_router
from app.api.studio.suggested_post_actions import router as suggested_post_actions_router
from app.bot.bot_instance import bot as tg_bot
from app.core.db import AsyncSessionLocal
from app.domain.content import (
    NATIVE_MEDIA_KINDS,
    NATIVE_MEDIA_OPTION_KEYS_BY_KIND,
    NATIVE_NESTED_BLOCK_TYPES,
    NATIVE_RICH_BLOCK_TYPES,
    NATIVE_RICH_MARK_TYPES,
    NATIVE_TELEGRAM_OPTION_KEYS,
    PostDocument,
    PostDocumentError,
)
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentNotFoundError, ContentRepo
from app.services.content import LegacyPayloadError, legacy_payload_from_document
from app.services.publication_bridge import LegacyPublicationBridge, PublicationBridgeError
from app.services.telegram_preview import TelegramPreviewError, TelegramPreviewService


async def _session_dependency() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_dependency)]
PrincipalDep = Annotated[StudioPrincipal, Depends(require_studio_principal)]


async def _client_for_principal(session: AsyncSession, principal: StudioPrincipal):
    return await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )


async def _owned_channel(session: AsyncSession, principal: StudioPrincipal, channel_id: int):
    client = await _client_for_principal(session, principal)
    channel = await ChannelsRepo(session).get_by_id(int(channel_id))
    if channel is None or int(channel.owner_id) != int(client.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return client, channel


def _content_summary(item) -> ContentSummaryResponse:
    return ContentSummaryResponse(
        id=int(item.id),
        channel_id=int(item.channel_id),
        kind=str(item.kind),
        status=str(item.status),
        title=item.title,
        current_revision=int(item.current_revision or 0),
        updated_at=item.updated_at,
    )


def create_studio_app(config: StudioConfig | None = None) -> FastAPI:
    cfg = config or studio_config
    app = FastAPI(
        title="Telegram Studio API",
        version="0.1.0",
        docs_url="/docs" if cfg.enabled else None,
        redoc_url=None,
    )
    app.include_router(planner_router)
    app.include_router(sources_router)
    app.include_router(media_assets_router)
    app.include_router(candidate_actions_router)
    app.include_router(channel_dm_replies_router)
    app.include_router(suggested_post_actions_router)
    app.include_router(candidate_rewrite_authority_router)
    app.include_router(candidate_prompt_rewrite_router)
    app.include_router(candidate_media_router)
    app.include_router(source_media_assets_router)
    app.include_router(channel_onboarding_router)

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "OPTIONS"],
            allow_headers=["Content-Type", "X-Telegram-Init-Data"],
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/studio/capabilities")
    async def capabilities(_principal: PrincipalDep) -> dict[str, object]:
        return {
            "post_document_schema_version": 1,
            "document_modes": ["classic", "rich"],
            "legacy_publisher": True,
            "rich_publisher": True,
            "rich_blocks": sorted(NATIVE_RICH_BLOCK_TYPES),
            "rich_nested_blocks": sorted(NATIVE_NESTED_BLOCK_TYPES),
            "rich_marks": sorted(NATIVE_RICH_MARK_TYPES),
            "rich_media_assets": True,
            "rich_media_asset_kinds": sorted(NATIVE_MEDIA_KINDS),
            "rich_media_options": {
                kind: sorted(options)
                for kind, options in sorted(NATIVE_MEDIA_OPTION_KEYS_BY_KIND.items())
            },
            "rich_media_attachments": True,
            "telegram_document_options": sorted(NATIVE_TELEGRAM_OPTION_KEYS),
            "exact_telegram_preview": True,
            "revisions": True,
            "planner": True,
            "sources_v2": True,
            "source_kinds": ["telegram", "rss", "url"],
            "candidate_to_draft": True,
        }

    @app.get("/api/studio/me", response_model=StudioUserResponse)
    async def me(principal: PrincipalDep, session: SessionDep) -> StudioUserResponse:
        await _client_for_principal(session, principal)
        return StudioUserResponse(
            tg_user_id=principal.tg_user_id,
            username=principal.username,
            full_name=principal.full_name,
        )

    @app.get("/api/studio/channels", response_model=list[ChannelResponse])
    async def channels(
        principal: PrincipalDep, session: SessionDep
    ) -> list[ChannelResponse]:
        client = await _client_for_principal(session, principal)
        rows = await ChannelsRepo(session).list_by_owner(client.id)
        return [
            ChannelResponse(
                id=int(row.id),
                tg_chat_id=int(row.tg_chat_id),
                title=row.title,
                is_active=bool(row.is_active),
            )
            for row in rows
        ]

    @app.get(
        "/api/studio/channels/{channel_id}/content",
        response_model=list[ContentSummaryResponse],
    )
    async def list_content(
        channel_id: int,
        principal: PrincipalDep,
        session: SessionDep,
        status_filter: str | None = None,
        limit: int = 50,
    ) -> list[ContentSummaryResponse]:
        await _owned_channel(session, principal, channel_id)
        rows = await ContentRepo(session).list_by_channel(
            channel_id,
            status=status_filter,
            limit=limit,
        )
        return [_content_summary(row) for row in rows]

    @app.post(
        "/api/studio/channels/{channel_id}/content",
        response_model=ContentDetailResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_content(
        channel_id: int,
        request: ContentCreateRequest,
        principal: PrincipalDep,
        session: SessionDep,
    ) -> ContentDetailResponse:
        await _owned_channel(session, principal, channel_id)
        try:
            document = PostDocument.from_dict(request.document)
            item = await ContentRepo(session).create(
                channel_id=channel_id,
                document=document,
                kind=request.kind,
                title=request.title,
                created_by_tg_user_id=principal.tg_user_id,
                source="studio",
            )
        except PostDocumentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ContentDetailResponse(
            **_content_summary(item).model_dump(),
            document=document.to_dict(),
        )

    @app.get(
        "/api/studio/channels/{channel_id}/content/{content_item_id}",
        response_model=ContentDetailResponse,
    )
    async def get_content(
        channel_id: int,
        content_item_id: int,
        principal: PrincipalDep,
        session: SessionDep,
    ) -> ContentDetailResponse:
        await _owned_channel(session, principal, channel_id)
        repo = ContentRepo(session)
        item = await repo.get_for_channel(content_item_id, channel_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Content not found")
        document = await repo.get_document(item.id)
        if document is None:
            raise HTTPException(status_code=409, detail="Content has no current revision")
        return ContentDetailResponse(
            **_content_summary(item).model_dump(),
            document=document.to_dict(),
        )

    @app.post(
        "/api/studio/channels/{channel_id}/content/{content_item_id}/revisions",
        response_model=ContentDetailResponse,
    )
    async def create_revision(
        channel_id: int,
        content_item_id: int,
        request: ContentRevisionRequest,
        principal: PrincipalDep,
        session: SessionDep,
    ) -> ContentDetailResponse:
        await _owned_channel(session, principal, channel_id)
        repo = ContentRepo(session)
        item = await repo.get_for_channel(content_item_id, channel_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Content not found")
        try:
            document = PostDocument.from_dict(request.document)
            await repo.append_revision(
                item.id,
                document,
                created_by_tg_user_id=principal.tg_user_id,
                source=request.source,
                status=request.status,
            )
        except PostDocumentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ContentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Content not found") from exc
        item = await repo.get(item.id)
        assert item is not None
        return ContentDetailResponse(
            **_content_summary(item).model_dump(),
            document=document.to_dict(),
        )

    @app.post("/api/studio/preview", response_model=PreviewResponse)
    async def preview(
        request: PreviewRequest,
        _principal: PrincipalDep,
    ) -> PreviewResponse:
        try:
            document = PostDocument.from_dict(request.document)
        except PostDocumentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            payload = legacy_payload_from_document(document)
            return PreviewResponse(
                mode=document.mode,
                primary_text=document.primary_text(),
                legacy_payload=payload,
                publishable_via_legacy=True,
            )
        except LegacyPayloadError as exc:
            return PreviewResponse(
                mode=document.mode,
                primary_text=document.primary_text(),
                legacy_payload=None,
                publishable_via_legacy=False,
                reason=str(exc),
            )

    @app.post(
        "/api/studio/preview/telegram",
        response_model=TelegramPreviewResponse,
    )
    async def exact_telegram_preview(
        request: TelegramPreviewRequest,
        principal: PrincipalDep,
        session: SessionDep,
    ) -> TelegramPreviewResponse:
        if request.channel_id is not None:
            await _owned_channel(session, principal, request.channel_id)
        try:
            document = PostDocument.from_dict(request.document)
        except PostDocumentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            message_ids = await TelegramPreviewService(tg_bot, AsyncSessionLocal).send(
                tg_user_id=principal.tg_user_id,
                document=document,
                replace_message_ids=request.replace_message_ids,
                asset_channel_id=request.channel_id,
            )
        except TelegramPreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return TelegramPreviewResponse(message_ids=message_ids)

    @app.post(
        "/api/studio/channels/{channel_id}/content/{content_item_id}/schedule",
        response_model=PublicationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def schedule_content(
        channel_id: int,
        content_item_id: int,
        request: ScheduleRequest,
        principal: PrincipalDep,
        session: SessionDep,
    ) -> PublicationResponse:
        await _owned_channel(session, principal, channel_id)
        repo = ContentRepo(session)
        item = await repo.get_for_channel(content_item_id, channel_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Content not found")
        repeat_rule = (
            {"enabled": True, "seconds": int(request.repeat_seconds)}
            if request.repeat_seconds
            else None
        )
        try:
            publication = await LegacyPublicationBridge(session).queue(
                content_item_id=item.id,
                scheduled_at=request.scheduled_at,
                content_revision=request.content_revision,
                timezone_name=request.timezone,
                repeat_rule=repeat_rule,
                runtime_options=request.runtime_options,
                metadata={"scheduled_from": "studio", "tg_user_id": principal.tg_user_id},
            )
        except PublicationBridgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return PublicationResponse(
            id=int(publication.id),
            content_item_id=int(publication.content_item_id),
            content_revision=int(publication.content_revision),
            channel_id=int(publication.channel_id),
            status=str(publication.status),
            schedule_entry_id=(
                int(publication.schedule_entry_id)
                if publication.schedule_entry_id is not None
                else None
            ),
            legacy_post_task_id=(
                int(publication.legacy_post_task_id)
                if publication.legacy_post_task_id is not None
                else None
            ),
        )

    return app
