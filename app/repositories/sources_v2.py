from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.models import ContentCandidate, SourceConnector, SourceDocument


class SourcesRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_connector(self, connector_id: int) -> SourceConnector | None:
        return await self.session.get(SourceConnector, int(connector_id))

    async def get_connector_for_channel(
        self, connector_id: int, channel_id: int
    ) -> SourceConnector | None:
        result = await self.session.execute(
            select(SourceConnector).where(
                SourceConnector.id == int(connector_id),
                SourceConnector.channel_id == int(channel_id),
            )
        )
        return result.scalar_one_or_none()

    async def list_connectors(self, channel_id: int) -> list[SourceConnector]:
        result = await self.session.execute(
            select(SourceConnector)
            .where(SourceConnector.channel_id == int(channel_id))
            .order_by(SourceConnector.id.asc())
        )
        return list(result.scalars().all())

    async def create_connector(
        self,
        *,
        channel_id: int,
        kind: str,
        value: str,
        mode: str = "research",
        citation_enabled: bool = True,
        reuse_policy: str = "reference_only",
        config: Mapping[str, Any] | None = None,
        legacy_ai_source_id: int | None = None,
        legacy_grab_source_id: int | None = None,
    ) -> SourceConnector:
        row = SourceConnector(
            channel_id=int(channel_id),
            kind=str(kind),
            value=str(value),
            mode=str(mode),
            citation_enabled=bool(citation_enabled),
            reuse_policy=str(reuse_policy),
            config=dict(config or {}),
            legacy_ai_source_id=legacy_ai_source_id,
            legacy_grab_source_id=legacy_grab_source_id,
        )
        self.session.add(row)
        try:
            await self.session.commit()
            await self.session.refresh(row)
            return row
        except Exception:
            await self.session.rollback()
            raise

    async def update_health(
        self,
        connector: SourceConnector,
        *,
        status: str,
        reason: str | None = None,
        auth_state: str | None = None,
        health: Mapping[str, Any] | None = None,
        success: bool = False,
    ) -> SourceConnector:
        now = datetime.now(timezone.utc)
        connector.status = str(status)
        connector.status_reason = reason
        if auth_state is not None:
            connector.auth_state = str(auth_state)
        if health is not None:
            connector.health = dict(health)
        if success:
            connector.last_success_at = now
        elif status in {"broken", "auth_required", "degraded"}:
            connector.last_error_at = now
        try:
            await self.session.commit()
            await self.session.refresh(connector)
            return connector
        except Exception:
            await self.session.rollback()
            raise

    async def get_document_by_identity(
        self,
        *,
        connector_id: int,
        external_id: str,
    ) -> SourceDocument | None:
        result = await self.session.execute(
            select(SourceDocument).where(
                SourceDocument.connector_id == int(connector_id),
                SourceDocument.external_id == str(external_id),
            )
        )
        return result.scalar_one_or_none()

    async def add_document(self, row: SourceDocument) -> SourceDocument:
        """Stage a source document and assign its identity without committing."""
        self.session.add(row)
        await self.session.flush()
        return row

    async def upsert_document(
        self,
        *,
        connector: SourceConnector,
        external_id: str,
        content: str,
        source_url: str | None = None,
        title: str | None = None,
        language: str | None = None,
        author: str | None = None,
        published_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[SourceDocument, bool]:
        """Compatibility API for explicit Studio/admin writes.

        Atomic ingestion uses get/add methods through SourceIngestionReconciliationService;
        this helper intentionally keeps the older commit-on-success contract for callers
        that create a standalone source document outside that reconciliation transaction.
        """
        normalized_content = str(content).strip()
        content_hash = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
        row = await self.get_document_by_identity(
            connector_id=int(connector.id),
            external_id=str(external_id),
        )
        created = row is None
        if row is None:
            row = SourceDocument(
                connector_id=int(connector.id),
                channel_id=int(connector.channel_id),
                external_id=str(external_id),
                content=normalized_content,
                content_hash=content_hash,
            )
            self.session.add(row)
        else:
            row.content = normalized_content
            row.content_hash = content_hash
        row.source_url = source_url
        row.title = title
        row.language = language
        row.author = author
        row.published_at = published_at
        row.fetched_at = datetime.now(timezone.utc)
        row.meta = dict(metadata or {})
        connector.last_document_at = row.published_at or row.fetched_at
        connector.last_success_at = datetime.now(timezone.utc)
        connector.status = "healthy"
        connector.status_reason = None
        try:
            await self.session.commit()
            await self.session.refresh(row)
            return row, created
        except Exception:
            await self.session.rollback()
            raise

    async def get_candidate_by_source_channel(
        self,
        *,
        source_document_id: int,
        channel_id: int,
    ) -> ContentCandidate | None:
        result = await self.session.execute(
            select(ContentCandidate).where(
                ContentCandidate.source_document_id == int(source_document_id),
                ContentCandidate.channel_id == int(channel_id),
            )
        )
        return result.scalar_one_or_none()

    async def add_candidate(self, row: ContentCandidate) -> ContentCandidate:
        """Stage a candidate and assign its identity without committing."""
        self.session.add(row)
        await self.session.flush()
        return row

    async def ensure_candidate(
        self,
        *,
        source_document_id: int,
        channel_id: int,
        status: str = "new",
        suggested_action: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContentCandidate:
        """Compatibility API for explicit candidate creation outside ingestion."""
        row = await self.get_candidate_by_source_channel(
            source_document_id=int(source_document_id),
            channel_id=int(channel_id),
        )
        if row is None:
            row = ContentCandidate(
                source_document_id=int(source_document_id),
                channel_id=int(channel_id),
                status=str(status),
                suggested_action=suggested_action,
                meta=dict(metadata or {}),
            )
            self.session.add(row)
            try:
                await self.session.commit()
                await self.session.refresh(row)
            except Exception:
                await self.session.rollback()
                raise
        return row

    async def list_documents(
        self, channel_id: int, *, limit: int = 100
    ) -> list[SourceDocument]:
        result = await self.session.execute(
            select(SourceDocument)
            .where(SourceDocument.channel_id == int(channel_id))
            .order_by(
                SourceDocument.published_at.desc().nullslast(),
                SourceDocument.fetched_at.desc(),
                SourceDocument.id.desc(),
            )
            .limit(max(1, min(int(limit), 500)))
        )
        return list(result.scalars().all())

    async def get_candidate_for_channel(
        self, candidate_id: int, channel_id: int
    ) -> ContentCandidate | None:
        result = await self.session.execute(
            select(ContentCandidate).where(
                ContentCandidate.id == int(candidate_id),
                ContentCandidate.channel_id == int(channel_id),
            )
        )
        return result.scalar_one_or_none()

    async def list_candidate_rows(
        self,
        channel_id: int,
        *,
        status: str | None = "new",
        limit: int = 100,
    ) -> list[tuple[ContentCandidate, SourceDocument]]:
        statement = (
            select(ContentCandidate, SourceDocument)
            .join(
                SourceDocument,
                SourceDocument.id == ContentCandidate.source_document_id,
            )
            .where(ContentCandidate.channel_id == int(channel_id))
        )
        if status is not None:
            statement = statement.where(ContentCandidate.status == str(status))
        statement = statement.order_by(
            SourceDocument.published_at.desc().nullslast(),
            ContentCandidate.id.desc(),
        ).limit(max(1, min(int(limit), 500)))
        result = await self.session.execute(statement)
        return [(candidate, document) for candidate, document in result.all()]

    async def set_candidate_status(
        self,
        candidate: ContentCandidate,
        status: str,
    ) -> ContentCandidate:
        candidate.status = str(status)
        try:
            await self.session.commit()
            await self.session.refresh(candidate)
            return candidate
        except Exception:
            await self.session.rollback()
            raise