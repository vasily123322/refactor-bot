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


_TABLE = "posting_dedupe_locks"
_COLUMN = "posting_dedupe_key"
_CONSTRAINT = "uq_publication_posting_dedupe"


def _publication_schema_state() -> tuple[set[str], set[str]]:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("publications")}
    uniques = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints("publications")
        if constraint.get("name")
    }
    return columns, uniques


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # Stable rows in this table are mutex identities, not execution owners. Scheduling
    # locks one row before re-checking both the legacy and canonical owner stores, so a
    # canonical-vs-legacy race cannot bypass the former global PostTask UNIQUE boundary.
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("dedupe_key", sa.String(length=255), primary_key=True),
        )

    # Historical Publications stay NULL. New direct canonical occurrences keep their
    # caller dedupe identity durably for lookup and an additional same-store UNIQUE
    # defense; compatibility PostTask retains its existing unique dedupe key.
    columns, uniques = _publication_schema_state()
    add_column = _COLUMN not in columns
    add_constraint = _CONSTRAINT not in uniques
    if add_column or add_constraint:
        with op.batch_alter_table("publications") as batch_op:
            if add_column:
                batch_op.add_column(
                    sa.Column(_COLUMN, sa.String(length=255), nullable=True)
                )
            if add_constraint:
                batch_op.create_unique_constraint(_CONSTRAINT, [_COLUMN])


def downgrade() -> None:
    columns, uniques = _publication_schema_state()
    drop_constraint = _CONSTRAINT in uniques
    drop_column = _COLUMN in columns
    if drop_constraint or drop_column:
        with op.batch_alter_table("publications") as batch_op:
            if drop_constraint:
                batch_op.drop_constraint(_CONSTRAINT, type_="unique")
            if drop_column:
                batch_op.drop_column(_COLUMN)

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
