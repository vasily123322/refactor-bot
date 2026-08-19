"""Add exact canonical repeat successor lineage.

Revision ID: 20260816_0010
Revises: 20260816_0009
Create Date: 2026-08-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260816_0010"
down_revision = "20260816_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing transitional successors remain NULL. New canonical continuation uses
    # this durable source identity instead of a compatibility PostTask dedupe key.
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(
            sa.Column("repeat_source_publication_id", sa.Integer(), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_publication_repeat_source",
            ["repeat_source_publication_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_constraint(
            "uq_publication_repeat_source",
            type_="unique",
        )
        batch_op.drop_column("repeat_source_publication_id")
