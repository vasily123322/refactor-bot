"""Add persisted publication execution mode.

Revision ID: 20260816_0009
Revises: 20260814_0008
Create Date: 2026-08-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260816_0009"
down_revision = "20260814_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(sa.Column("execution_mode", sa.String(length=32), nullable=True))
        batch_op.create_check_constraint(
            "ck_publication_execution_mode",
            "execution_mode IS NULL OR execution_mode IN ('canonical', 'intentional_legacy')",
        )


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_constraint("ck_publication_execution_mode", type_="check")
        batch_op.drop_column("execution_mode")
