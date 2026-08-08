from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ContentItem(Base):
    """Stable content identity independent from schedule/publication attempts."""

    __tablename__ = "content_items"
    __table_args__ = (
        Index("ix_content_items_channel_status", "channel_id", "status"),
        Index("ix_content_items_channel_updated", "channel_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="post")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContentRevision(Base):
    """Immutable snapshot of a ContentItem's PostDocument."""

    __tablename__ = "content_revisions"
    __table_args__ = (
        UniqueConstraint(
            "content_item_id", "revision", name="uq_content_revision_number"
        ),
        Index("ix_content_revisions_item_created", "content_item_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), default="editor")
    created_by_tg_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MediaAsset(Base):
    """Reusable media reference shared by editor, AI generation and publishing."""

    __tablename__ = "media_assets"
    __table_args__ = (
        Index("ix_media_assets_channel_created", "channel_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), default="telegram")
    telegram_file_id: Mapped[str | None] = mapped_column(String(1024))
    storage_url: Mapped[str | None] = mapped_column(String(2048))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
