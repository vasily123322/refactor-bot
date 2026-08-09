"""Add durable scheduler task execution leases.

Revision ID: 20260809_0002
Revises: 20260809_0001
Create Date: 2026-08-09
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260809_0002"
down_revision: Union[str, Sequence[str], None] = "20260809_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Legacy/unmanaged startup still calls Base.metadata.create_all(). Until that
    # compatibility path is retired, it may have created this table before Alembic
    # adoption. IF NOT EXISTS lets the explicit revision safely take ownership.
    op.create_table(
        "scheduler_task_leases",
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("holder", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["task_id"], ["post_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("lease_token", name="uq_scheduler_task_lease_token"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_scheduler_task_leases_expires_at",
        "scheduler_task_leases",
        ["expires_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduler_task_leases_expires_at",
        table_name="scheduler_task_leases",
        if_exists=True,
    )
    op.drop_table("scheduler_task_leases", if_exists=True)
