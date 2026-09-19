"""Add durable bounded admin-agent run/event history.

Revision ID: 20260919_0016
Revises: 20260919_0015
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260919_0016"
down_revision = "20260919_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "channel_id",
            sa.Integer(),
            sa.ForeignKey("channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scenario", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_admin_agent_runs_channel_id", "admin_agent_runs", ["channel_id"])
    op.create_index("ix_admin_agent_runs_owner_tg_user_id", "admin_agent_runs", ["owner_tg_user_id"])
    op.create_index("ix_admin_agent_runs_scenario", "admin_agent_runs", ["scenario"])
    op.create_index("ix_admin_agent_runs_status", "admin_agent_runs", ["status"])
    op.create_index(
        "ix_admin_agent_runs_channel_created",
        "admin_agent_runs",
        ["channel_id", "created_at"],
    )
    op.create_index(
        "ix_admin_agent_runs_owner_created",
        "admin_agent_runs",
        ["owner_tg_user_id", "created_at"],
    )

    op.create_table(
        "admin_agent_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("admin_agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_admin_agent_event_sequence"),
    )
    op.create_index("ix_admin_agent_events_run_id", "admin_agent_events", ["run_id"])
    op.create_index("ix_admin_agent_events_event_type", "admin_agent_events", ["event_type"])
    op.create_index(
        "ix_admin_agent_events_run_sequence",
        "admin_agent_events",
        ["run_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_table("admin_agent_events")
    op.drop_table("admin_agent_runs")
