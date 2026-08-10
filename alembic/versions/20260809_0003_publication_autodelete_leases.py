"""Add durable canonical publication autodelete leases.

Revision ID: 20260809_0003
Revises: 20260809_0002
Create Date: 2026-08-10
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260809_0003"
down_revision: Union[str, Sequence[str], None] = "20260809_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Legacy/unmanaged startup still calls Base.metadata.create_all(). Until that
    # compatibility path is retired, it may have pre-created this table. Keep this
    # revision adoptable so Alembic can take ownership without destructive rebuilds.
    op.create_table(
        "publication_autodelete_leases",
        sa.Column("publication_id", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["publication_id"], ["publications.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("publication_id"),
        sa.UniqueConstraint(
            "lease_token", name="uq_publication_autodelete_lease_token"
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_publication_autodelete_leases_expires_at",
        "publication_autodelete_leases",
        ["expires_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_publication_autodelete_leases_expires_at",
        table_name="publication_autodelete_leases",
        if_exists=True,
    )
    op.drop_table("publication_autodelete_leases", if_exists=True)
