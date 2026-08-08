from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision, MediaAsset
from app.services.content import document_from_legacy_payload


class ContentNotFoundError(LookupError):
    pass


def _document_dict(document: PostDocument | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(document, PostDocument):
        return document.to_dict()
    return PostDocument.from_dict(document).to_dict()


class ContentRepo:
    """Persistence boundary for versioned editor content."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        channel_id: int,
        document: PostDocument | Mapping[str, Any],
        kind: str = "post",
        status: str = "draft",
        title: str | None = None,
        created_by_tg_user_id: int | None = None,
        source: str = "editor",
        metadata: Mapping[str, Any] | None = None,
    ) -> ContentItem:
        doc = _document_dict(document)
        item = ContentItem(
            channel_id=int(channel_id),
            kind=str(kind),
            status=str(status),
            title=title,
            current_revision=0,
            meta=dict(metadata or {}),
        )
        self.session.add(item)
        try:
            await self.session.flush()
            revision = ContentRevision(
                content_item_id=int(item.id),
                revision=1,
                document=doc,
                source=str(source),
                created_by_tg_user_id=created_by_tg_user_id,
                meta={},
            )
            self.session.add(revision)
            item.current_revision = 1
            await self.session.commit()
            await self.session.refresh(item)
            return item
        except Exception:
            await self.session.rollback()
            raise

    async def create_from_legacy_payload(
        self,
        *,
        channel_id: int,
        payload: Mapping[str, Any],
        status: str = "draft",
        title: str | None = None,
        created_by_tg_user_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContentItem:
        return await self.create(
            channel_id=channel_id,
            document=document_from_legacy_payload(payload),
            status=status,
            title=title,
            created_by_tg_user_id=created_by_tg_user_id,
            source="legacy_import",
            metadata=metadata,
        )

    async def get(self, content_item_id: int) -> ContentItem | None:
        return await self.session.get(ContentItem, int(content_item_id))

    async def get_for_channel(
        self, content_item_id: int, channel_id: int
    ) -> ContentItem | None:
        result = await self.session.execute(
            select(ContentItem).where(
                ContentItem.id == int(content_item_id),
                ContentItem.channel_id == int(channel_id),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_channel(
        self,
        channel_id: int,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[ContentItem]:
        stmt = select(ContentItem).where(ContentItem.channel_id == int(channel_id))
        if status is not None:
            stmt = stmt.where(ContentItem.status == str(status))
        stmt = stmt.order_by(ContentItem.updated_at.desc(), ContentItem.id.desc()).limit(
            max(1, min(int(limit), 200))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_revision(
        self, content_item_id: int, revision: int | None = None
    ) -> ContentRevision | None:
        if revision is None:
            item = await self.get(content_item_id)
            if item is None or int(item.current_revision or 0) <= 0:
                return None
            revision = int(item.current_revision)
        result = await self.session.execute(
            select(ContentRevision).where(
                ContentRevision.content_item_id == int(content_item_id),
                ContentRevision.revision == int(revision),
            )
        )
        return result.scalar_one_or_none()

    async def get_document(
        self, content_item_id: int, revision: int | None = None
    ) -> PostDocument | None:
        row = await self.get_revision(content_item_id, revision)
        return PostDocument.from_dict(row.document) if row is not None else None

    async def append_revision(
        self,
        content_item_id: int,
        document: PostDocument | Mapping[str, Any],
        *,
        created_by_tg_user_id: int | None = None,
        source: str = "editor",
        metadata: Mapping[str, Any] | None = None,
        status: str | None = None,
    ) -> ContentRevision:
        doc = _document_dict(document)
        try:
            result = await self.session.execute(
                select(ContentItem)
                .where(ContentItem.id == int(content_item_id))
                .with_for_update()
            )
            item = result.scalar_one_or_none()
            if item is None:
                raise ContentNotFoundError(f"content item {content_item_id} not found")

            next_revision = int(item.current_revision or 0) + 1
            row = ContentRevision(
                content_item_id=int(item.id),
                revision=next_revision,
                document=doc,
                source=str(source),
                created_by_tg_user_id=created_by_tg_user_id,
                meta=dict(metadata or {}),
            )
            self.session.add(row)
            item.current_revision = next_revision
            if status is not None:
                item.status = str(status)
            await self.session.commit()
            # Both rows can contain server-generated/onupdate values. Refresh them
            # explicitly so API/service callers never trigger implicit async IO from
            # plain attribute access (MissingGreenlet under AsyncSession).
            await self.session.refresh(row)
            await self.session.refresh(item)
            return row
        except Exception:
            await self.session.rollback()
            raise

    async def set_status(self, content_item_id: int, status: str) -> ContentItem:
        item = await self.get(content_item_id)
        if item is None:
            raise ContentNotFoundError(f"content item {content_item_id} not found")
        item.status = str(status)
        try:
            await self.session.commit()
            await self.session.refresh(item)
            return item
        except Exception:
            await self.session.rollback()
            raise


class MediaAssetsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        channel_id: int,
        kind: str,
        source: str = "telegram",
        telegram_file_id: str | None = None,
        storage_url: str | None = None,
        mime_type: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_seconds: int | None = None,
        size_bytes: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MediaAsset:
        asset = MediaAsset(
            channel_id=int(channel_id),
            kind=str(kind),
            source=str(source),
            telegram_file_id=telegram_file_id,
            storage_url=storage_url,
            mime_type=mime_type,
            width=width,
            height=height,
            duration_seconds=duration_seconds,
            size_bytes=size_bytes,
            meta=dict(metadata or {}),
        )
        self.session.add(asset)
        try:
            await self.session.commit()
            await self.session.refresh(asset)
            return asset
        except Exception:
            await self.session.rollback()
            raise

    async def list_by_channel(self, channel_id: int, *, limit: int = 100) -> list[MediaAsset]:
        result = await self.session.execute(
            select(MediaAsset)
            .where(MediaAsset.channel_id == int(channel_id))
            .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
            .limit(max(1, min(int(limit), 500)))
        )
        return list(result.scalars().all())
