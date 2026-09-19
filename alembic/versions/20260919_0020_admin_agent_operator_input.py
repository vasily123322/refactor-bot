"""Add normalized operator input to admin-agent runs.

Revision ID: 20260919_0020
Revises: 20260919_0019
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260919_0020"
down_revision = "20260919_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_agent_runs",
        sa.Column("operator_input", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("admin_agent_runs", "operator_input")
