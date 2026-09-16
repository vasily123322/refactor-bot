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


_COLUMN = "execution_mode"
_CONSTRAINT = "ck_publication_execution_mode"


def _schema_state() -> tuple[set[str], set[str]]:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("publications")}
    checks = {
        constraint.get("name")
        for constraint in inspector.get_check_constraints("publications")
        if constraint.get("name")
    }
    return columns, checks


def upgrade() -> None:
    columns, checks = _schema_state()
    add_column = _COLUMN not in columns
    add_constraint = _CONSTRAINT not in checks
    if not add_column and not add_constraint:
        return

    with op.batch_alter_table("publications") as batch_op:
        if add_column:
            batch_op.add_column(sa.Column(_COLUMN, sa.String(length=32), nullable=True))
        if add_constraint:
            batch_op.create_check_constraint(
                _CONSTRAINT,
                "execution_mode IS NULL OR execution_mode IN ('canonical', 'intentional_legacy')",
            )


def downgrade() -> None:
    columns, checks = _schema_state()
    drop_constraint = _CONSTRAINT in checks
    drop_column = _COLUMN in columns
    if not drop_constraint and not drop_column:
        return

    with op.batch_alter_table("publications") as batch_op:
        if drop_constraint:
            batch_op.drop_constraint(_CONSTRAINT, type_="check")
        if drop_column:
            batch_op.drop_column(_COLUMN)
