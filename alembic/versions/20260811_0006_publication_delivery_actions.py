"""Add durable canonical publication delivery action reservations.

Revision ID: 20260811_0006
Revises: 20260811_0005
Create Date: 2026-08-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260811_0006"
down_revision: Union[str, Sequence[str], None] = "20260811_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Transitional unmanaged startup may pre-create this ORM table through
    # Base.metadata.create_all(). Keep the Alembic revision adoptable.
    op.create_table(
        "publication_delivery_actions",
        sa.Column("publication_id", sa.Integer(), nullable=False),
        sa.Column("action_key", sa.String(length=160), nullable=False),
        sa.Column("action_type", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("intent_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reserved_by_lease_token", sa.String(length=64), nullable=False),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action_type IN ('pin', 'forward')",
            name="ck_publication_delivery_action_type",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'succeeded', 'unknown', 'suppressed')",
            name="ck_publication_delivery_action_state",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"], ["publications.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("publication_id", "action_key"),
        if_not_exists=True,
    )


def downgrade() -> None:
    # Pin/forward rows are durable provider-call evidence. Removing a populated ledger
    # would discard reserved/unknown no-replay barriers and could authorize a duplicate
    # provider effect after a later upgrade. Only an empty ledger is safely reversible.
    existing = op.get_bind().execute(
        sa.text("SELECT 1 FROM publication_delivery_actions LIMIT 1")
    ).first()
    if existing is not None:
        raise RuntimeError(
            "refusing unsafe downgrade: publication_delivery_actions contains "
            "durable no-replay evidence"
        )

    op.drop_table("publication_delivery_actions", if_exists=True)
