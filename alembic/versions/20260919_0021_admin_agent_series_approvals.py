"""Add durable campaign scheduling approval batches.

Revision ID: 20260919_0021
Revises: 20260919_0020
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260919_0021"
down_revision = "20260919_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_agent_approval_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("source_run_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("series_title", sa.String(length=255), nullable=False),
        sa.Column("source_plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("execution_key", sa.String(length=64), nullable=False),
        sa.Column("reviewer_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("execution_claim_token", sa.String(length=64), nullable=True),
        sa.Column("execution_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending_review','executing','executed','rejected','stale','partial_failed','failed')",
            name="ck_admin_agent_approval_batch_state",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"], ["admin_agent_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_tg_user_id", "channel_id", "action_type", "request_id",
            name="uq_admin_agent_approval_batch_request",
        ),
    )
    op.create_index("ix_admin_agent_approval_batches_channel_created", "admin_agent_approval_batches", ["channel_id", "created_at"], unique=False)
    op.create_index("ix_admin_agent_approval_batches_owner_state", "admin_agent_approval_batches", ["owner_tg_user_id", "state"], unique=False)
    op.create_index("ix_admin_agent_approval_batches_execution_key", "admin_agent_approval_batches", ["execution_key"], unique=True)
    op.create_index(op.f("ix_admin_agent_approval_batches_owner_tg_user_id"), "admin_agent_approval_batches", ["owner_tg_user_id"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batches_channel_id"), "admin_agent_approval_batches", ["channel_id"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batches_source_run_id"), "admin_agent_approval_batches", ["source_run_id"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batches_action_type"), "admin_agent_approval_batches", ["action_type"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batches_state"), "admin_agent_approval_batches", ["state"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batches_action_fingerprint"), "admin_agent_approval_batches", ["action_fingerprint"], unique=False)

    op.create_table(
        "admin_agent_approval_batch_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content_item_id", sa.Integer(), nullable=False),
        sa.Column("captured_content_revision", sa.Integer(), nullable=False),
        sa.Column("content_title", sa.String(length=255), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("local_time", sa.String(length=5), nullable=False),
        sa.Column("resolved_scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("item_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("execution_key", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("schedule_entry_id", sa.Integer(), nullable=True),
        sa.Column("publication_id", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','executing','executed','stale','failed')",
            name="ck_admin_agent_approval_batch_item_state",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["admin_agent_approval_batches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "ordinal", name="uq_admin_agent_approval_batch_item_ordinal"),
        sa.UniqueConstraint("batch_id", "content_item_id", name="uq_admin_agent_approval_batch_item_content"),
        sa.UniqueConstraint("execution_key", name="uq_admin_agent_approval_batch_item_execution_key"),
    )
    op.create_index("ix_admin_agent_approval_batch_items_batch_ordinal", "admin_agent_approval_batch_items", ["batch_id", "ordinal"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batch_items_batch_id"), "admin_agent_approval_batch_items", ["batch_id"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batch_items_content_item_id"), "admin_agent_approval_batch_items", ["content_item_id"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batch_items_item_fingerprint"), "admin_agent_approval_batch_items", ["item_fingerprint"], unique=False)
    op.create_index(op.f("ix_admin_agent_approval_batch_items_state"), "admin_agent_approval_batch_items", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_agent_approval_batch_items_state"), table_name="admin_agent_approval_batch_items")
    op.drop_index(op.f("ix_admin_agent_approval_batch_items_item_fingerprint"), table_name="admin_agent_approval_batch_items")
    op.drop_index(op.f("ix_admin_agent_approval_batch_items_content_item_id"), table_name="admin_agent_approval_batch_items")
    op.drop_index(op.f("ix_admin_agent_approval_batch_items_batch_id"), table_name="admin_agent_approval_batch_items")
    op.drop_index("ix_admin_agent_approval_batch_items_batch_ordinal", table_name="admin_agent_approval_batch_items")
    op.drop_table("admin_agent_approval_batch_items")

    op.drop_index(op.f("ix_admin_agent_approval_batches_action_fingerprint"), table_name="admin_agent_approval_batches")
    op.drop_index(op.f("ix_admin_agent_approval_batches_state"), table_name="admin_agent_approval_batches")
    op.drop_index(op.f("ix_admin_agent_approval_batches_action_type"), table_name="admin_agent_approval_batches")
    op.drop_index(op.f("ix_admin_agent_approval_batches_source_run_id"), table_name="admin_agent_approval_batches")
    op.drop_index(op.f("ix_admin_agent_approval_batches_channel_id"), table_name="admin_agent_approval_batches")
    op.drop_index(op.f("ix_admin_agent_approval_batches_owner_tg_user_id"), table_name="admin_agent_approval_batches")
    op.drop_index("ix_admin_agent_approval_batches_execution_key", table_name="admin_agent_approval_batches")
    op.drop_index("ix_admin_agent_approval_batches_owner_state", table_name="admin_agent_approval_batches")
    op.drop_index("ix_admin_agent_approval_batches_channel_created", table_name="admin_agent_approval_batches")
    op.drop_table("admin_agent_approval_batches")
