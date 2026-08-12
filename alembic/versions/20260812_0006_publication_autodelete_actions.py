"""Add durable per-message time-autodelete action authority.

Revision ID: 20260812_0006
Revises: 20260811_0005
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260812_0006"
down_revision = "20260811_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_autodelete_actions",
        sa.Column("publication_id", sa.Integer(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("authority_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reservation_token", sa.String(length=64), nullable=False),
        sa.Column("reserved_by_lease_token", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            "state IN ('reserved', 'succeeded', 'unavailable', 'unknown')",
            name="ck_publication_autodelete_actions_state",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "publication_id",
            "telegram_message_id",
        ),
        sa.UniqueConstraint(
            "reservation_token",
            name="uq_publication_autodelete_action_reservation_token",
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    # These rows are irreversible-provider evidence. Dropping a non-empty ledger would
    # erase reserved/unknown no-replay barriers and could make a later upgrade delete a
    # Telegram message again after an ambiguous prior invocation. Permit a reversible
    # schema round-trip only while the ledger is empty; populated ledgers require an
    # explicit operator reconciliation/export rather than a silent destructive downgrade.
    existing = op.get_bind().execute(
        sa.text("SELECT 1 FROM publication_autodelete_actions LIMIT 1")
    ).first()
    if existing is not None:
        raise RuntimeError(
            "refusing unsafe downgrade: publication_autodelete_actions contains "
            "destructive no-replay evidence"
        )

    op.drop_table(
        "publication_autodelete_actions",
        if_exists=True,
    )
