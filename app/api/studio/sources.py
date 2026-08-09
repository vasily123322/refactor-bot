from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.core.db import AsyncSessionLocal
from app.domain.models import AISource
from app.domain.sources.models import ContentCandidate, SourceConnector, SourceDocument
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.sources_v2 import SourcesRepo
from app.services.legacy_source_mirror import LegacySourceMirror
from app.services.source_doctor import SourceDoctor
from app.services.source_ingestion import SourceIngestionError, SourceIngestionService
from app.services.source_ingestion_lease import SourceIngestionLeaseService
from app.services.source_lifecycle import (
    SourceLifecycleError,
    SourceLifecyclePatch,
    SourceLifecycleService,
)
from app.services.source_worker_policy import (
    clear_source_worker_failure,
    source_worker_failure_count,
    source_worker_retry_after,
)
from app.services.telegram_source_ingestion import (
    TelegramSourceIngestionService,
    telegram_backlog_hint,
    telegram_cursor_message_id,
)
from app.userbot.client import app as userbot


router = APIRouter(prefix="/api/studio", tags=["sources"])


class SourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["telegram", "rss", "url"]
    value: str = Field(min_length=1, max_length=1024)
    mode: Literal["summary", "rewrite"] = "summary"
    citation_enabled: bool = True
    reuse_policy: Literal[
        "reference_only",
        "summarize",
        "quote_with_attribution",
        "rewrite_with_attribution",
        "mirror_authorized",
    ] = "reference_only"


class SourceSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    mode: Literal["summary", "rewrite"] | None = None
    citation_enabled: bool | None = None
    reuse_policy: Literal[
        "reference_only",
        "summarize",
        "quote_with_attribution",
        "rewrite_with_attribution",
        "mirror_authorized",
    ] | None = None


class SourceResponse(BaseModel):
    id: int
    channel_id: int
    kind: str
    value: str
    enabled: bool
    mode: str
    citation_enabled: bool
    reuse_policy: str
    status: str
    status_reason: str | None
    auth_state: str
    capabilities: dict[str, Any]
    health: dict[str, Any]
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_document_at: datetime | None
    legacy_ai_source_id: int | None
    legacy_grab_source_id: int | None
    cursor_message_id: int | None
    backlog_hint: bool
    worker_failure_count: int
    worker_retry_after: datetime | None


class SourceIngestionResponse(BaseModel):
    connector_id: int
    documents_seen: int
    documents_created: int
    candidates_created: int


class CandidateResponse(BaseModel):
    id: int
    source_document_id: int
    connector_id: int
    status: str
    suggested_action: str | None
    score: float | None
    topic: str | None
    summary: str | None
    source_title: str | None
    source_url: str | None
    excerpt: str
    published_at: datetime | None
    fetched_at: datetime | None
    created_at: datetime | None
    reuse_policy: str


async def _session_dependency() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_dependency)]
PrincipalDep = Annotated[StudioPrincipal, Depends(require_studio_principal)]


async def _require_owned_channel(
    session: AsyncSession, principal: StudioPrincipal, channel_id: int
) -> None:
    client = await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )
    channel = await ChannelsRepo(session).get_by_id(int(channel_id))
    if channel is None or int(channel.owner_id) != int(client.id):
        raise HTTPException(status_code=404, detail="Channel not found")


async def _release_ingestion_lease(lease) -> None:
    # Never reuse the ingestion transaction for lease cleanup. Otherwise a finally
    # block could accidentally commit dirty state left by a failed adapter.
    try:
        async with AsyncSessionLocal() as lease_session:
            await SourceIngestionLeaseService(lease_session).release(lease)
    except Exception:
        # TTL recovery is the fallback if cleanup itself loses database connectivity.
        pass


def _response(row: SourceConnector) -> SourceResponse:
    is_telegram = str(row.kind).lower() == "telegram"
    cursor = telegram_cursor_message_id(row) if is_telegram else 0
    return SourceResponse(
        id=int(row.id),
        channel_id=int(row.channel_id),
        kind=str(row.kind),
        value=str(row.value),
        enabled=bool(row.enabled),
        mode=str(row.mode),
        citation_enabled=bool(row.citation_enabled),
        reuse_policy=str(row.reuse_policy),
        status=str(row.status),
        status_reason=row.status_reason,
        auth_state=str(row.auth_state),
        capabilities=dict(row.capabilities or {}),
        health=dict(row.health or {}),
        last_success_at=row.last_success_at,
        last_error_at=row.last_error_at,
        last_document_at=row.last_document_at,
        legacy_ai_source_id=row.legacy_ai_source_id,
        legacy_grab_source_id=row.legacy_grab_source_id,
        cursor_message_id=(cursor or None) if is_telegram else None,
        backlog_hint=telegram_backlog_hint(row) if is_telegram else False,
        worker_failure_count=source_worker_failure_count(row),
        worker_retry_after=source_worker_retry_after(row),
    )


def _excerpt(value: str, *, limit: int = 700) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _candidate_response(
    candidate: ContentCandidate,
    document: SourceDocument,
) -> CandidateResponse:
    metadata = dict(candidate.meta or {})
    document_meta = dict(document.meta or {})
    return CandidateResponse(
        id=int(candidate.id),
        source_document_id=int(document.id),
        connector_id=int(document.connector_id),
        status=str(candidate.status),
        suggested_action=candidate.suggested_action,
        score=candidate.score,
        topic=candidate.topic,
        summary=candidate.summary,
        source_title=document.title,
        source_url=document.source_url,
        excerpt=_excerpt(document.content),
        published_at=document.published_at,
        fetched_at=document.fetched_at,
        created_at=candidate.created_at,
        reuse_policy=str(
            metadata.get("reuse_policy")
            or document_meta.get("reuse_policy")
            or "reference_only"
        ),
    )


@router.get(
    "/channels/{channel_id}/sources",
    response_model=list[SourceResponse],
)
async def list_sources(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> list[SourceResponse]:
    await _require_owned_channel(session, principal, channel_id)
    await LegacySourceMirror(session).sync_channel(channel_id)
    rows = await SourcesRepo(session).list_connectors(channel_id)
    doctor = SourceDoctor()
    for row in rows:
        if not row.capabilities:
            row.capabilities = doctor.capabilities(row.kind)
    return [_response(row) for row in rows]


@router.post(
    "/channels/{channel_id}/sources",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_source(
    channel_id: int,
    request: SourceCreateRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> SourceResponse:
    await _require_owned_channel(session, principal, channel_id)
    value = request.value.strip()
    existing = await SourcesRepo(session).list_connectors(channel_id)
    if any(
        row.kind == request.kind and row.value.casefold() == value.casefold()
        for row in existing
    ):
        raise HTTPException(status_code=409, detail="Source already exists")

    legacy = AISource(
        channel_id=int(channel_id),
        source_type=request.kind,
        source_value=value,
        mode=request.mode,
        enabled=True,
        citation_enabled=request.citation_enabled,
    )
    session.add(legacy)
    try:
        await session.flush()
        row = SourceConnector(
            channel_id=int(channel_id),
            kind=request.kind,
            value=value,
            enabled=True,
            mode=request.mode,
            citation_enabled=request.citation_enabled,
            reuse_policy=request.reuse_policy,
            status="unknown",
            auth_state=("session_required" if request.kind == "telegram" else "not_required"),
            capabilities=SourceDoctor.capabilities(request.kind),
            config={"created_from": "studio"},
            legacy_ai_source_id=int(legacy.id),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    except Exception:
        await session.rollback()
        raise
    return _response(row)


@router.post(
    "/channels/{channel_id}/sources/{connector_id}/settings",
    response_model=SourceResponse,
)
async def update_source_settings(
    channel_id: int,
    connector_id: int,
    request: SourceSettingsRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> SourceResponse:
    await _require_owned_channel(session, principal, channel_id)
    patch = SourceLifecyclePatch(**request.model_dump(exclude_none=True))
    try:
        row = await SourceLifecycleService(session).update(
            channel_id=channel_id,
            connector_id=connector_id,
            patch=patch,
        )
    except SourceLifecycleError as exc:
        if str(exc) == "source not found":
            raise HTTPException(status_code=404, detail="Source not found") from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _response(row)


@router.post(
    "/channels/{channel_id}/sources/{connector_id}/doctor",
    response_model=SourceResponse,
)
async def check_source(
    channel_id: int,
    connector_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> SourceResponse:
    await _require_owned_channel(session, principal, channel_id)
    repo = SourcesRepo(session)
    row = await repo.get_connector_for_channel(connector_id, channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    doctor = SourceDoctor(telegram_probe=userbot.get_chat)
    result = await doctor.check(row)
    row.capabilities = dict(result.capabilities)
    row = await repo.update_health(
        row,
        status=result.status,
        reason=result.reason,
        auth_state=result.auth_state,
        health=result.health,
        success=result.success,
    )
    return _response(row)


@router.post(
    "/channels/{channel_id}/sources/{connector_id}/ingest",
    response_model=SourceIngestionResponse,
)
async def ingest_source(
    channel_id: int,
    connector_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> SourceIngestionResponse:
    await _require_owned_channel(session, principal, channel_id)
    row = await SourcesRepo(session).get_connector_for_channel(connector_id, channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")

    lease = await SourceIngestionLeaseService(session).acquire(
        connector_id=int(row.id),
        holder="studio",
    )
    if lease is None:
        raise HTTPException(status_code=409, detail="Source ingestion already running")

    try:
        if str(row.kind).lower() == "telegram":
            result = await TelegramSourceIngestionService(session).ingest(row)
        elif str(row.kind).lower() in {"rss", "url", "web"}:
            result = await SourceIngestionService(session).ingest(row)
        else:
            raise HTTPException(status_code=409, detail="Unsupported source adapter")

        if clear_source_worker_failure(row):
            await session.commit()
        return SourceIngestionResponse(
            connector_id=result.connector_id,
            documents_seen=result.documents_seen,
            documents_created=result.documents_created,
            candidates_created=result.candidates_created,
        )
    except SourceIngestionError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        raise
    finally:
        await _release_ingestion_lease(lease)


@router.get(
    "/channels/{channel_id}/candidates",
    response_model=list[CandidateResponse],
)
async def list_candidates(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    status_filter: Literal["new", "dismissed"] | None = "new",
    limit: int = 100,
) -> list[CandidateResponse]:
    await _require_owned_channel(session, principal, channel_id)
    rows = await SourcesRepo(session).list_candidate_rows(
        channel_id,
        status=status_filter,
        limit=limit,
    )
    return [_candidate_response(candidate, document) for candidate, document in rows]


@router.post(
    "/channels/{channel_id}/candidates/{candidate_id}/dismiss",
    response_model=CandidateResponse,
)
async def dismiss_candidate(
    channel_id: int,
    candidate_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> CandidateResponse:
    await _require_owned_channel(session, principal, channel_id)
    repo = SourcesRepo(session)
    candidate = await repo.get_candidate_for_channel(candidate_id, channel_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    document = await session.get(SourceDocument, int(candidate.source_document_id))
    if document is None or int(document.channel_id) != int(channel_id):
        raise HTTPException(status_code=404, detail="Candidate source not found")
    candidate = await repo.set_candidate_status(candidate, "dismissed")
    return _candidate_response(candidate, document)
