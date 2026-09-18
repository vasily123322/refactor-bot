from __future__ import annotations

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .mixins import TimestampHelpersMixin
from .models import CreatedAtMixin


class SourceConnector(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "source_connectors"
    __table_args__ = (
        Index("ix_source_connector_channel_status", "channel_id", "status"),
        UniqueConstraint("legacy_ai_source_id", name="uq_source_connector_ai_source"),
        UniqueConstraint("legacy_grab_source_id", name="uq_source_connector_grab_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[str] = mapped_column(String(1024))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(32), default="research")
    citation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reuse_policy: Mapped[str] = mapped_column(String(32), default="reference_only")
    status: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    auth_state: Mapped[str] = mapped_column(String(24), default="not_required")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    health: Mapped[dict] = mapped_column(JSON, default=dict)
    last_success_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    last_document_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    legacy_ai_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    legacy_grab_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SourceDocument(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint(
            "connector_id", "external_id", name="uq_source_document_external"
        ),
        Index("ix_source_document_channel_published", "channel_id", "published_at"),
        Index("ix_source_document_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    connector_id: Mapped[int] = mapped_column(
        ForeignKey("source_connectors.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    language: Mapped[str | None] = mapped_column(String(16))
    author: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class ContentCandidate(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "content_candidates"
    __table_args__ = (
        UniqueConstraint(
            "source_document_id", "channel_id", name="uq_candidate_source_channel"
        ),
        Index("ix_candidate_channel_status", "channel_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    score: Mapped[float | None] = mapped_column(Float)
    topic: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    suggested_action: Mapped[str | None] = mapped_column(String(32))
    content_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True
    )
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
