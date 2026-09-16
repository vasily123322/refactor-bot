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


_COLUMN = "repeat_source_publication_id"
_CONSTRAINT = "uq_publication_repeat_source"


def _schema_state() -> tuple[set[str], set[str]]:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("publications")}
    uniques = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints("publications")
        if constraint.get("name")
    }
    return columns, uniques


def upgrade() -> None:
    # Existing transitional successors remain NULL. New canonical continuation uses
    # this durable source identity instead of a compatibility PostTask dedupe key.
    columns, uniques = _schema_state()
    add_column = _COLUMN not in columns
    add_constraint = _CONSTRAINT not in uniques
    if not add_column and not add_constraint:
        return

    with op.batch_alter_table("publications") as batch_op:
        if add_column:
            batch_op.add_column(sa.Column(_COLUMN, sa.Integer(), nullable=True))
        if add_constraint:
            batch_op.create_unique_constraint(_CONSTRAINT, [_COLUMN])


def downgrade() -> None:
    columns, uniques = _schema_state()
    drop_constraint = _CONSTRAINT in uniques
    drop_column = _COLUMN in columns
    if not drop_constraint and not drop_column:
        return

    with op.batch_alter_table("publications") as batch_op:
        if drop_constraint:
            batch_op.drop_constraint(_CONSTRAINT, type_="unique")
        if drop_column:
            batch_op.drop_column(_COLUMN)
