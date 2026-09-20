"""Add durable approval execution fencing.

Revision ID: 20260920_0025
Revises: 20260920_0024
Create Date: 2026-09-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260920_0025"
down_revision = "20260920_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_agent_execution_fences",
        sa.Column("fence_key", sa.String(length=255), nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("fence_key"),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO admin_agent_execution_fences (fence_key, claim_token)
            SELECT
                'approval-execution:' || execution_key,
                execution_claim_token
            FROM admin_agent_approvals
            WHERE state = 'executing'
              AND execution_key IS NOT NULL
              AND execution_claim_token IS NOT NULL
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO admin_agent_execution_fences (fence_key, claim_token)
            SELECT
                'approval-execution:' || execution_key,
                execution_claim_token
            FROM admin_agent_approval_batches
            WHERE state = 'executing'
              AND execution_key IS NOT NULL
              AND execution_claim_token IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_table("admin_agent_execution_fences")
