from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.models import ContentCandidate, SourceConnector, SourceDocument
from app.repositories.sources_v2 import SourcesRepo


class SourceProjectionUpdateMode(str, Enum):
    CONTENT = "content"
    LIFECYCLE = "lifecycle"


@dataclass(frozen=True, slots=True)
class SourceProjection:
    """Transport-neutral projection of one stable external source identity.

    Internal routing is intentionally absent. The trusted SourceConnector supplied
    to the reconciliation service is the only channel authority.
    """

    external_id: str
    content: str | None = None
    source_url: str | None = None
    title: str | None = None
    language: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    update_mode: SourceProjectionUpdateMode = SourceProjectionUpdateMode.CONTENT


@dataclass(frozen=True, slots=True)
class SourceReconciliationResult:
    document: SourceDocument
    candidate: ContentCandidate
    document_created: bool
    candidate_created: bool


class SourceReconciliationError(RuntimeError):
    pass


def _candidate_action(connector: SourceConnector) -> str:
    mode = str(connector.mode or "research")
    if mode == "summary":
        return "summarize"
    if mode == "rewrite":
        return "rewrite"
    if mode == "mirror":
        return "mirror" if connector.reuse_policy == "mirror_authorized" else "review"
    return "research"


def _merge_metadata(
    current: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = {str(key): value for key, value in dict(current or {}).items()}
    merged.update({str(key): value for key, value in dict(incoming or {}).items()})
    return dict(sorted(merged.items()))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SourceIngestionReconciliationService:
    """Atomically reconcile one source projection and its canonical candidate."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = SourcesRepo(session)

    async def _load_or_create_document(
        self,
        *,
        connector: SourceConnector,
        external_id: str,
        projection: SourceProjection,
        now: datetime,
    ) -> tuple[SourceDocument, bool]:
        row = await self.repo.get_document_by_identity(
            connector_id=int(connector.id),
            external_id=external_id,
        )
        if row is not None:
            return row, False
        if projection.update_mode is SourceProjectionUpdateMode.LIFECYCLE:
            raise SourceReconciliationError(
                "lifecycle projection cannot create a missing source document"
            )
        normalized_content = str(projection.content or "").strip()
        if not normalized_content:
            raise SourceReconciliationError("content projection must contain source content")
        candidate = SourceDocument(
            connector_id=int(connector.id),
            channel_id=int(connector.channel_id),
            external_id=external_id,
            content=normalized_content,
            content_hash=hashlib.sha256(normalized_content.encode("utf-8")).hexdigest(),
            source_url=projection.source_url,
            title=projection.title,
            language=projection.language,
            author=projection.author,
            published_at=projection.published_at,
            fetched_at=now,
            meta=_merge_metadata(None, projection.metadata),
        )
        try:
            async with self.session.begin_nested():
                await self.repo.add_document(candidate)
            return candidate, True
        except IntegrityError:
            winner = await self.repo.get_document_by_identity(
                connector_id=int(connector.id),
                external_id=external_id,
            )
            if winner is None:
                raise SourceReconciliationError(
                    "source document unique conflict did not resolve to a winner"
                )
            return winner, False

    async def _load_or_create_candidate(
        self,
        *,
        connector: SourceConnector,
        document: SourceDocument,
    ) -> tuple[ContentCandidate, bool]:
        row = await self.repo.get_candidate_by_source_channel(
            source_document_id=int(document.id),
            channel_id=int(connector.channel_id),
        )
        if row is not None:
            return row, False
        candidate = ContentCandidate(
            source_document_id=int(document.id),
            channel_id=int(connector.channel_id),
            status="new",
            suggested_action=_candidate_action(connector),
            meta={
                "reuse_policy": str(connector.reuse_policy),
                "source_connector_id": int(connector.id),
            },
        )
        try:
            async with self.session.begin_nested():
                await self.repo.add_candidate(candidate)
            return candidate, True
        except IntegrityError:
            winner = await self.repo.get_candidate_by_source_channel(
                source_document_id=int(document.id),
                channel_id=int(connector.channel_id),
            )
            if winner is None:
                raise SourceReconciliationError(
                    "content candidate unique conflict did not resolve to a winner"
                )
            return winner, False

    def _apply_projection(
        self,
        *,
        connector: SourceConnector,
        document: SourceDocument,
        projection: SourceProjection,
        now: datetime,
    ) -> None:
        if projection.update_mode is SourceProjectionUpdateMode.CONTENT:
            normalized_content = str(projection.content or "").strip()
            if not normalized_content:
                raise SourceReconciliationError(
                    "content projection must contain source content"
                )
            document.channel_id = int(connector.channel_id)
            document.content = normalized_content
            document.content_hash = hashlib.sha256(
                normalized_content.encode("utf-8")
            ).hexdigest()
            document.source_url = projection.source_url
            document.title = projection.title
            document.language = projection.language
            document.author = projection.author
            document.published_at = projection.published_at
        document.fetched_at = now
        document.meta = _merge_metadata(document.meta, projection.metadata)

    def _stage_connector_success(
        self,
        *,
        connector: SourceConnector,
        projection: SourceProjection,
        now: datetime,
    ) -> None:
        connector.status = "healthy"
        connector.status_reason = None
        connector.last_success_at = now
        if projection.update_mode is not SourceProjectionUpdateMode.CONTENT:
            return
        observed = projection.published_at or now
        if connector.last_document_at is None or _as_utc(observed) > _as_utc(
            connector.last_document_at
        ):
            connector.last_document_at = _as_utc(observed)

    async def reconcile(
        self,
        connector: SourceConnector,
        projection: SourceProjection,
    ) -> SourceReconciliationResult:
        external_id = str(projection.external_id).strip()
        if not external_id:
            raise SourceReconciliationError("source projection external_id must not be empty")
        if connector.id is None or connector.channel_id is None:
            raise SourceReconciliationError("trusted source connector must be persisted")

        now = datetime.now(timezone.utc)
        try:
            document, document_created = await self._load_or_create_document(
                connector=connector,
                external_id=external_id,
                projection=projection,
                now=now,
            )
            self._apply_projection(
                connector=connector,
                document=document,
                projection=projection,
                now=now,
            )
            candidate, candidate_created = await self._load_or_create_candidate(
                connector=connector,
                document=document,
            )
            candidate.meta = _merge_metadata(
                candidate.meta,
                {
                    "reuse_policy": str(connector.reuse_policy),
                    "source_connector_id": int(connector.id),
                },
            )
            if candidate.suggested_action is None:
                candidate.suggested_action = _candidate_action(connector)
            self._stage_connector_success(
                connector=connector,
                projection=projection,
                now=now,
            )
            await self.session.commit()
            return SourceReconciliationResult(
                document=document,
                candidate=candidate,
                document_created=document_created,
                candidate_created=candidate_created,
            )
        except Exception:
            await self.session.rollback()
            raise
