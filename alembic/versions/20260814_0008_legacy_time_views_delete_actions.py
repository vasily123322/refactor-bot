"""Add durable legacy mixed time+views delete action authority.

Revision ID: 20260814_0008
Revises: 20260812_0007
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260814_0008"
down_revision = "20260812_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legacy_time_views_delete_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_task_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=False),
        sa.Column("target_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reservation_token", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('reserved', 'succeeded', 'unknown')",
            name="ck_legacy_time_views_delete_action_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "post_task_id",
            name="uq_legacy_time_views_delete_action_post_task",
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    # These rows are irreversible-provider evidence. Dropping a non-empty ledger
    # would erase reserved/unknown no-replay barriers. Permit schema rollback only
    # while the destructive ledger is empty.
    existing = op.get_bind().execute(
        sa.text("SELECT 1 FROM legacy_time_views_delete_actions LIMIT 1")
    ).first()
    if existing is not None:
        raise RuntimeError(
            "refusing unsafe downgrade: legacy_time_views_delete_actions contains "
            "destructive no-replay evidence"
        )

    op.drop_table(
        "legacy_time_views_delete_actions",
        if_exists=True,
    )
