from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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

from app.core.db import Base


class ScheduleEntry(Base):
    """When a specific content revision should be delivered to a channel."""

    __tablename__ = "schedule_entries"
    __table_args__ = (
        Index("ix_schedule_entries_channel_time", "channel_id", "scheduled_at"),
        Index("ix_schedule_entries_status_time", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    content_revision: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    scheduled_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    timezone: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    repeat_rule: Mapped[dict] = mapped_column(JSON, default=dict)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Publication(Base):
    """Observable delivery state for one content revision and destination."""

    __tablename__ = "publications"
    __table_args__ = (
        Index("ix_publications_channel_status", "channel_id", "status"),
        Index("ix_publications_content", "content_item_id", "content_revision"),
        UniqueConstraint("legacy_post_task_id", name="uq_publication_legacy_task"),
        UniqueConstraint(
            "repeat_source_publication_id",
            name="uq_publication_repeat_source",
        ),
        CheckConstraint(
            "execution_mode IS NULL OR execution_mode IN ('canonical', 'intentional_legacy')",
            name="ck_publication_execution_mode",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    schedule_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedule_entries.id", ondelete="SET NULL"), index=True
    )
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    content_revision: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    execution_mode: Mapped[str | None] = mapped_column(String(32))
    repeat_source_publication_id: Mapped[int | None] = mapped_column(Integer)
    legacy_post_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("post_tasks.id", ondelete="SET NULL"), index=True
    )
    telegram_message_ids: Mapped[list[int] | None] = mapped_column(JSON)
    result_link: Mapped[str | None] = mapped_column(String(2048))
    last_error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PublicationAttempt(Base):
    """Append-only record of an actual delivery attempt."""

    __tablename__ = "publication_attempts"
    __table_args__ = (
        UniqueConstraint(
            "publication_id", "attempt", name="uq_publication_attempt_number"
        ),
        Index("ix_publication_attempts_pub_started", "publication_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    telegram_message_ids: Mapped[list[int] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
