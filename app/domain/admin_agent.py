from __future__ import annotations

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AdminAgentRun(Base):
    """Durable execution/audit record for one bounded Studio assistant request."""

    __tablename__ = "admin_agent_runs"
    __table_args__ = (
        Index("ix_admin_agent_runs_channel_created", "channel_id", "created_at"),
        Index("ix_admin_agent_runs_owner_created", "owner_tg_user_id", "created_at"),
        Index(
            "uq_admin_agent_run_idempotency",
            "owner_tg_user_id",
            "channel_id",
            "scenario",
            "request_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    scenario: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str | None] = mapped_column(String(128))
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdminAgentEvent(Base):
    """Bounded execution event; payloads contain operational metadata, never prompts/secrets."""

    __tablename__ = "admin_agent_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_admin_agent_event_sequence"),
        Index("ix_admin_agent_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("admin_agent_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
