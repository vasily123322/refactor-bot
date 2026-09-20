from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
        Index(
            "ix_admin_agent_runs_skill_version",
            "skill_id",
            "skill_version",
        ),
        UniqueConstraint(
            "automation_id",
            "scheduled_for",
            name="uq_admin_agent_run_automation_occurrence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    scenario: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str | None] = mapped_column(String(128))
    operator_input: Mapped[dict | None] = mapped_column(JSON)
    skill_id: Mapped[str | None] = mapped_column(String(64))
    skill_version: Mapped[str | None] = mapped_column(String(32))
    automation_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_agent_automations.id", ondelete="SET NULL"), index=True
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow_phase: Mapped[str | None] = mapped_column(String(64))
    checkpoint: Mapped[dict | None] = mapped_column(JSON)
    execution_claim_token: Mapped[str | None] = mapped_column(String(64))
    execution_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class AdminAgentAutomation(Base):
    """Durable bounded recurring definition for one exact Admin Agent skill version."""

    __tablename__ = "admin_agent_automations"
    __table_args__ = (
        UniqueConstraint(
            "owner_tg_user_id",
            "channel_id",
            "request_id",
            name="uq_admin_agent_automation_request",
        ),
        CheckConstraint(
            "cadence_kind IN ('daily','weekly')",
            name="ck_admin_agent_automation_cadence_kind",
        ),
        CheckConstraint(
            "(cadence_kind = 'daily' AND weekday IS NULL) OR "
            "(cadence_kind = 'weekly' AND weekday BETWEEN 0 AND 6)",
            name="ck_admin_agent_automation_weekday",
        ),
        CheckConstraint(
            "disabled_reason IS NULL OR disabled_reason IN "
            "('manual_pause','ownership_lost','unsupported_skill_version','invalid_definition')",
            name="ck_admin_agent_automation_disabled_reason",
        ),
        CheckConstraint(
            "last_outcome IS NULL OR last_outcome IN "
            "('run_recorded','misfire_skipped','safety_disabled')",
            name="ck_admin_agent_automation_last_outcome",
        ),
        Index(
            "ix_admin_agent_automations_due",
            "enabled",
            "next_run_at",
        ),
        Index(
            "ix_admin_agent_automations_owner_channel",
            "owner_tg_user_id",
            "channel_id",
        ),
        Index(
            "ix_admin_agent_automations_skill_version",
            "skill_id",
            "skill_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    skill_id: Mapped[str] = mapped_column(String(64))
    skill_version: Mapped[str] = mapped_column(String(32))
    operator_input: Mapped[dict] = mapped_column(JSON, default=dict)
    cadence_kind: Mapped[str] = mapped_column(String(16))
    local_time: Mapped[str] = mapped_column(String(5))
    weekday: Mapped[int | None] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(64))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outcome: Mapped[str | None] = mapped_column(String(64))
    last_outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[str] = mapped_column(String(128))
    definition_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminAgentRunArtifact(Base):
    """Durable canonical artifact ownership for a resumable admin-agent run."""

    __tablename__ = "admin_agent_run_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "artifact_type",
            "ordinal",
            name="uq_admin_agent_run_artifact_ordinal",
        ),
        UniqueConstraint(
            "run_id",
            "content_item_id",
            name="uq_admin_agent_run_artifact_content",
        ),
        Index(
            "ix_admin_agent_run_artifacts_run",
            "run_id",
            "artifact_type",
            "ordinal",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("admin_agent_runs.id", ondelete="CASCADE"), index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    content_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
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


class AdminAgentExecutionFence(Base):
    """Durable token fence held through approval canonical scheduling commits."""

    __tablename__ = "admin_agent_execution_fences"

    fence_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    claim_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminAgentApproval(Base):
    """Durable, server-authored approval snapshot for one bounded mutation intent."""

    __tablename__ = "admin_agent_approvals"
    __table_args__ = (
        UniqueConstraint(
            "owner_tg_user_id",
            "channel_id",
            "action_type",
            "request_id",
            name="uq_admin_agent_approval_request",
        ),
        CheckConstraint(
            "state IN ('pending_review','executing','executed','rejected','stale','failed')",
            name="ck_admin_agent_approval_state",
        ),
        Index(
            "ix_admin_agent_approvals_channel_created",
            "channel_id",
            "created_at",
        ),
        Index(
            "ix_admin_agent_approvals_owner_state",
            "owner_tg_user_id",
            "state",
        ),
        Index(
            "ix_admin_agent_approvals_execution_key",
            "execution_key",
            unique=True,
        ),
        Index(
            "uq_admin_agent_approval_active_target",
            "owner_tg_user_id",
            "channel_id",
            "action_type",
            "content_item_id",
            "content_revision",
            unique=True,
            sqlite_where=text("state IN ('pending_review','executing')"),
            postgresql_where=text("state IN ('pending_review','executing')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    source_admin_agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_agent_runs.id", ondelete="SET NULL"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    content_item_id: Mapped[int] = mapped_column(Integer, index=True)
    content_revision: Mapped[int] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(64))
    target_local_date: Mapped[date] = mapped_column(Date)
    local_time: Mapped[str] = mapped_column(String(5))
    resolved_scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    execution_key: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(128))
    schedule_entry_id: Mapped[int | None] = mapped_column(Integer)
    publication_id: Mapped[int | None] = mapped_column(Integer)
    reviewer_tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    execution_claim_token: Mapped[str | None] = mapped_column(String(64))
    execution_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )



class AdminAgentApprovalBatch(Base):
    """Durable approval parent for one bounded campaign scheduling intent."""

    __tablename__ = "admin_agent_approval_batches"
    __table_args__ = (
        UniqueConstraint(
            "owner_tg_user_id",
            "channel_id",
            "action_type",
            "request_id",
            name="uq_admin_agent_approval_batch_request",
        ),
        CheckConstraint(
            "state IN ('pending_review','executing','executed','rejected','stale','partial_failed','failed')",
            name="ck_admin_agent_approval_batch_state",
        ),
        Index(
            "ix_admin_agent_approval_batches_channel_created",
            "channel_id",
            "created_at",
        ),
        Index(
            "ix_admin_agent_approval_batches_owner_state",
            "owner_tg_user_id",
            "state",
        ),
        Index(
            "ix_admin_agent_approval_batches_execution_key",
            "execution_key",
            unique=True,
        ),
        Index(
            "uq_admin_agent_approval_batch_active_source_run",
            "owner_tg_user_id",
            "channel_id",
            "action_type",
            "source_run_id",
            unique=True,
            sqlite_where=text("state IN ('pending_review','executing')"),
            postgresql_where=text("state IN ('pending_review','executing')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    source_run_id: Mapped[int] = mapped_column(
        ForeignKey("admin_agent_runs.id", ondelete="RESTRICT"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    request_id: Mapped[str] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(Integer)
    series_title: Mapped[str] = mapped_column(String(255))
    source_plan_fingerprint: Mapped[str] = mapped_column(String(64))
    action_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    execution_key: Mapped[str] = mapped_column(String(64))
    reviewer_tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    execution_claim_token: Mapped[str | None] = mapped_column(String(64))
    execution_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminAgentApprovalBatchItem(Base):
    """Ordered, server-authored scheduling intent inside one campaign approval."""

    __tablename__ = "admin_agent_approval_batch_items"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "ordinal", name="uq_admin_agent_approval_batch_item_ordinal"
        ),
        UniqueConstraint(
            "batch_id", "content_item_id", name="uq_admin_agent_approval_batch_item_content"
        ),
        UniqueConstraint(
            "execution_key", name="uq_admin_agent_approval_batch_item_execution_key"
        ),
        CheckConstraint(
            "state IN ('pending','executing','executed','stale','failed')",
            name="ck_admin_agent_approval_batch_item_state",
        ),
        Index(
            "ix_admin_agent_approval_batch_items_batch_ordinal",
            "batch_id",
            "ordinal",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("admin_agent_approval_batches.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    content_item_id: Mapped[int] = mapped_column(Integer, index=True)
    captured_content_revision: Mapped[int] = mapped_column(Integer)
    content_title: Mapped[str] = mapped_column(String(255))
    local_date: Mapped[date] = mapped_column(Date)
    local_time: Mapped[str] = mapped_column(String(5))
    resolved_scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    item_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    execution_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), index=True)
    schedule_entry_id: Mapped[int | None] = mapped_column(Integer)
    publication_id: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
