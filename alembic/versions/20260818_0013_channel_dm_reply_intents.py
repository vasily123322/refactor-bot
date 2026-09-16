"""Add durable Channel-DM reply intents.

Revision ID: 20260818_0013
Revises: 20260818_0012
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260818_0013"
down_revision = "20260818_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_dm_reply_intents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("owner_client_id", sa.Integer(), nullable=False),
        sa.Column("proposed_text", sa.Text(), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=24), nullable=False),
        sa.Column(
            "state",
            sa.String(length=24),
            nullable=False,
            server_default="pending_review",
        ),
        sa.Column("active_slot", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("generation_meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("handoff_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("consumed_command_id", sa.Integer(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["content_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["consumed_command_id"],
            ["channel_dm_reply_commands.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "owner_client_id",
            "active_slot",
            name="uq_channel_dm_reply_intent_active_candidate_owner",
        ),
        sa.UniqueConstraint(
            "handoff_idempotency_key",
            name="uq_channel_dm_reply_intent_handoff_key",
        ),
        sa.UniqueConstraint(
            "consumed_command_id",
            name="uq_channel_dm_reply_intent_consumed_command",
        ),
        sa.CheckConstraint(
            "state IN ('pending_review', 'dismissed', 'stale', 'consumed')",
            name="ck_channel_dm_reply_intent_state",
        ),
        sa.CheckConstraint(
            "origin IN ('manual', 'ai', 'automation')",
            name="ck_channel_dm_reply_intent_origin",
        ),
        sa.CheckConstraint(
            "(state = 'pending_review' AND active_slot = 1) OR "
            "(state <> 'pending_review' AND active_slot IS NULL)",
            name="ck_channel_dm_reply_intent_active_slot",
        ),
        sa.CheckConstraint(
            "(handoff_started_at IS NULL AND handoff_idempotency_key IS NULL) OR "
            "(handoff_started_at IS NOT NULL AND handoff_idempotency_key IS NOT NULL)",
            name="ck_channel_dm_reply_intent_handoff_pair",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_channel_dm_reply_intents_candidate_id",
        "channel_dm_reply_intents",
        ["candidate_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_channel_dm_reply_intents_source_document_id",
        "channel_dm_reply_intents",
        ["source_document_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_channel_dm_reply_intents_owner_client_id",
        "channel_dm_reply_intents",
        ["owner_client_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_channel_dm_reply_intents_state",
        "channel_dm_reply_intents",
        ["state"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_channel_dm_reply_intent_candidate_state",
        "channel_dm_reply_intents",
        ["candidate_id", "state", "id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_channel_dm_reply_intent_owner_state",
        "channel_dm_reply_intents",
        ["owner_client_id", "state", "id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_dm_reply_intent_owner_state",
        table_name="channel_dm_reply_intents",
        if_exists=True,
    )
    op.drop_index(
        "ix_channel_dm_reply_intent_candidate_state",
        table_name="channel_dm_reply_intents",
        if_exists=True,
    )
    op.drop_index(
        "ix_channel_dm_reply_intents_state",
        table_name="channel_dm_reply_intents",
        if_exists=True,
    )
    op.drop_index(
        "ix_channel_dm_reply_intents_owner_client_id",
        table_name="channel_dm_reply_intents",
        if_exists=True,
    )
    op.drop_index(
        "ix_channel_dm_reply_intents_source_document_id",
        table_name="channel_dm_reply_intents",
        if_exists=True,
    )
    op.drop_index(
        "ix_channel_dm_reply_intents_candidate_id",
        table_name="channel_dm_reply_intents",
        if_exists=True,
    )
    op.drop_table("channel_dm_reply_intents", if_exists=True)
