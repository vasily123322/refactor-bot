"""Preserve global posting dedupe across PostTask-free scheduling.

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
    # Stable rows in this table are mutex identities, not execution owners. Scheduling
    # locks one row before re-checking both the legacy and canonical owner stores, so a
    # canonical-vs-legacy race cannot bypass the former global PostTask UNIQUE boundary.
    op.create_table(
        "posting_dedupe_locks",
        sa.Column("dedupe_key", sa.String(length=255), primary_key=True),
    )

    # Historical Publications stay NULL. New direct canonical occurrences keep their
    # caller dedupe identity durably for lookup and an additional same-store UNIQUE
    # defense; compatibility PostTask retains its existing unique dedupe key.
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
    op.drop_table("posting_dedupe_locks")
