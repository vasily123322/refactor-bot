"""Add durable direct canonical posting dedupe key.

Revision ID: 20260816_0010a
Revises: 20260816_0010
Create Date: 2026-08-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260816_0010a"
down_revision = "20260816_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical rows stay NULL. New direct canonical scheduling persists the caller's
    # dedupe identity here so the PostTask-free path retains the old DB-global UNIQUE
    # idempotency boundary.
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(
            sa.Column("posting_dedupe_key", sa.String(length=255), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_publication_posting_dedupe",
            ["posting_dedupe_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_constraint(
            "uq_publication_posting_dedupe",
            type_="unique",
        )
        batch_op.drop_column("posting_dedupe_key")
