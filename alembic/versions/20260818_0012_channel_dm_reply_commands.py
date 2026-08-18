"""Add durable Channel-DM reply commands.

Revision ID: 20260818_0012
Revises: 20260817_0011
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260818_0012"
down_revision = "20260817_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_dm_reply_commands",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("reply_text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("provider_message_id", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["candidate_id"], ["content_candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "candidate_id",
            "idempotency_key",
            name="uq_channel_dm_reply_candidate_idempotency",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'sent', 'failed', 'uncertain')",
            name="ck_channel_dm_reply_state",
        ),
    )
    op.create_index(
        "ix_channel_dm_reply_commands_candidate_id",
        "channel_dm_reply_commands",
        ["candidate_id"],
    )
    op.create_index(
        "ix_channel_dm_reply_commands_source_document_id",
        "channel_dm_reply_commands",
        ["source_document_id"],
    )
    op.create_index(
        "ix_channel_dm_reply_commands_state",
        "channel_dm_reply_commands",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index("ix_channel_dm_reply_commands_state", table_name="channel_dm_reply_commands")
    op.drop_index(
        "ix_channel_dm_reply_commands_source_document_id",
        table_name="channel_dm_reply_commands",
    )
    op.drop_index(
        "ix_channel_dm_reply_commands_candidate_id",
        table_name="channel_dm_reply_commands",
    )
    op.drop_table("channel_dm_reply_commands")
