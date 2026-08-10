"""Add indexed canonical views-based autodelete scheduler state.

Revision ID: 20260810_0004
Revises: 20260809_0003
Create Date: 2026-08-10
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260810_0004"
down_revision: Union[str, Sequence[str], None] = "20260809_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Legacy/unmanaged startup still calls Base.metadata.create_all(). Keep this
    # revision adoptable when that path pre-creates the new scheduler state table.
    op.create_table(
        "publication_autodelete_view_states",
        sa.Column("publication_id", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("last_views", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint(
            "threshold > 0",
            name="ck_publication_autodelete_view_states_threshold_positive",
        ),
        sa.CheckConstraint(
            "last_views IS NULL OR last_views >= 0",
            name="ck_publication_autodelete_view_states_last_views_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"], ["publications.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("publication_id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_publication_autodelete_view_states_next_check_at",
        "publication_autodelete_view_states",
        ["next_check_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_publication_autodelete_view_states_next_check_at",
        table_name="publication_autodelete_view_states",
        if_exists=True,
    )
    op.drop_table("publication_autodelete_view_states", if_exists=True)
