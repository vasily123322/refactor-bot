"""Add admin-agent draft request idempotency.

Revision ID: 20260919_0017
Revises: 20260919_0016
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260919_0017"
down_revision = "20260919_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_agent_runs",
        sa.Column("request_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "uq_admin_agent_run_idempotency",
        "admin_agent_runs",
        ["owner_tg_user_id", "channel_id", "scenario", "request_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_admin_agent_run_idempotency", table_name="admin_agent_runs")
    op.drop_column("admin_agent_runs", "request_id")
