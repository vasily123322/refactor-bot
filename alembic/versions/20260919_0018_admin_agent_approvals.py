"""Add durable admin-agent approvals for bounded scheduling.

Revision ID: 20260919_0018
Revises: 20260919_0017
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260919_0018"
down_revision = "20260919_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_agent_approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("source_admin_agent_run_id", sa.Integer(), nullable=True),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("content_item_id", sa.Integer(), nullable=False),
        sa.Column("content_revision", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("target_local_date", sa.Date(), nullable=False),
        sa.Column("local_time", sa.String(length=5), nullable=False),
        sa.Column("resolved_scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("execution_key", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("schedule_entry_id", sa.Integer(), nullable=True),
        sa.Column("publication_id", sa.Integer(), nullable=True),
        sa.Column("reviewer_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("execution_claim_token", sa.String(length=64), nullable=True),
        sa.Column("execution_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending_review','executing','executed','rejected','stale','failed')",
            name="ck_admin_agent_approval_state",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            name="fk_admin_agent_approvals_channel_id_channels",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_admin_agent_run_id"],
            ["admin_agent_runs.id"],
            name="fk_admin_agent_approvals_source_run_admin_agent_runs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_tg_user_id",
            "channel_id",
            "action_type",
            "request_id",
            name="uq_admin_agent_approval_request",
        ),
    )
    op.create_index(
        "ix_admin_agent_approvals_channel_created",
        "admin_agent_approvals",
        ["channel_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_agent_approvals_owner_state",
        "admin_agent_approvals",
        ["owner_tg_user_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_admin_agent_approvals_execution_key",
        "admin_agent_approvals",
        ["execution_key"],
        unique=True,
    )
    op.create_index(
        op.f("ix_admin_agent_approvals_owner_tg_user_id"),
        "admin_agent_approvals",
        ["owner_tg_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_approvals_channel_id"),
        "admin_agent_approvals",
        ["channel_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_approvals_source_admin_agent_run_id"),
        "admin_agent_approvals",
        ["source_admin_agent_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_approvals_action_type"),
        "admin_agent_approvals",
        ["action_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_approvals_state"),
        "admin_agent_approvals",
        ["state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_approvals_content_item_id"),
        "admin_agent_approvals",
        ["content_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_approvals_action_fingerprint"),
        "admin_agent_approvals",
        ["action_fingerprint"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_agent_approvals_action_fingerprint"), table_name="admin_agent_approvals")
    op.drop_index(op.f("ix_admin_agent_approvals_content_item_id"), table_name="admin_agent_approvals")
    op.drop_index(op.f("ix_admin_agent_approvals_state"), table_name="admin_agent_approvals")
    op.drop_index(op.f("ix_admin_agent_approvals_action_type"), table_name="admin_agent_approvals")
    op.drop_index(op.f("ix_admin_agent_approvals_source_admin_agent_run_id"), table_name="admin_agent_approvals")
    op.drop_index(op.f("ix_admin_agent_approvals_channel_id"), table_name="admin_agent_approvals")
    op.drop_index(op.f("ix_admin_agent_approvals_owner_tg_user_id"), table_name="admin_agent_approvals")
    op.drop_index("ix_admin_agent_approvals_execution_key", table_name="admin_agent_approvals")
    op.drop_index("ix_admin_agent_approvals_owner_state", table_name="admin_agent_approvals")
    op.drop_index("ix_admin_agent_approvals_channel_created", table_name="admin_agent_approvals")
    op.drop_table("admin_agent_approvals")
