"""Add durable Studio native channel onboarding requests.

Revision ID: 20260817_0011
Revises: 20260816_0010
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260817_0011"
down_revision = "20260816_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_channel_onboarding_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("expected_tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("prepared_button_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="reserved"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selected_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("request_id", name="uq_studio_channel_onboarding_request_id"),
        sa.UniqueConstraint(
            "prepared_button_id",
            name="uq_studio_channel_onboarding_prepared_button_id",
        ),
    )
    op.create_index(
        "ix_studio_channel_onboarding_requests_request_id",
        "studio_channel_onboarding_requests",
        ["request_id"],
    )
    op.create_index(
        "ix_studio_channel_onboarding_requests_client_id",
        "studio_channel_onboarding_requests",
        ["client_id"],
    )
    op.create_index(
        "ix_studio_channel_onboarding_requests_expected_tg_user_id",
        "studio_channel_onboarding_requests",
        ["expected_tg_user_id"],
    )
    op.create_index(
        "ix_studio_channel_onboarding_requests_status",
        "studio_channel_onboarding_requests",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_studio_channel_onboarding_requests_status",
        table_name="studio_channel_onboarding_requests",
    )
    op.drop_index(
        "ix_studio_channel_onboarding_requests_expected_tg_user_id",
        table_name="studio_channel_onboarding_requests",
    )
    op.drop_index(
        "ix_studio_channel_onboarding_requests_client_id",
        table_name="studio_channel_onboarding_requests",
    )
    op.drop_index(
        "ix_studio_channel_onboarding_requests_request_id",
        table_name="studio_channel_onboarding_requests",
    )
    op.drop_table("studio_channel_onboarding_requests")