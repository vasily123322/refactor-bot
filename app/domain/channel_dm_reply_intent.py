from __future__ import annotations

from datetime import datetime
from typing import Any

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


class ChannelDMReplyIntent(Base):
    """Durable review/proposal authority for one ordinary Channel-DM reply.

    This row never owns Telegram routing or delivery state. Delivery truth lives in
    ChannelDMReplyCommand and is linked only after an explicit user handoff.
    """

    __tablename__ = "channel_dm_reply_intents"
    __table_args__ = (
        Index(
            "ix_channel_dm_reply_intent_candidate_state",
            "candidate_id",
            "state",
            "id",
        ),
        Index(
            "ix_channel_dm_reply_intent_owner_state",
            "owner_client_id",
            "state",
            "id",
        ),
        UniqueConstraint(
            "candidate_id",
            "owner_client_id",
            "active_slot",
            name="uq_channel_dm_reply_intent_active_candidate_owner",
        ),
        UniqueConstraint(
            "handoff_idempotency_key",
            name="uq_channel_dm_reply_intent_handoff_key",
        ),
        UniqueConstraint(
            "consumed_command_id",
            name="uq_channel_dm_reply_intent_consumed_command",
        ),
        CheckConstraint(
            "state IN ('pending_review', 'dismissed', 'stale', 'consumed')",
            name="ck_channel_dm_reply_intent_state",
        ),
        CheckConstraint(
            "origin IN ('manual', 'ai', 'automation')",
            name="ck_channel_dm_reply_intent_origin",
        ),
        CheckConstraint(
            "(state = 'pending_review' AND active_slot = 1) OR "
            "(state <> 'pending_review' AND active_slot IS NULL)",
            name="ck_channel_dm_reply_intent_active_slot",
        ),
        CheckConstraint(
            "(handoff_started_at IS NULL AND handoff_idempotency_key IS NULL) OR "
            "(handoff_started_at IS NOT NULL AND handoff_idempotency_key IS NOT NULL)",
            name="ck_channel_dm_reply_intent_handoff_pair",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("content_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposed_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending_review", index=True
    )
    active_slot: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    generation_meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    handoff_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    handoff_idempotency_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    consumed_command_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_dm_reply_commands.id", ondelete="SET NULL"),
        nullable=True,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
