"""Add canonical runtime safety audit.

Revision ID: 20260918_0014
Revises: 20260818_0013
Create Date: 2026-09-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260918_0014"
down_revision = "20260818_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Unmanaged legacy startup may pre-create current ORM tables before Alembic
    # adoption. Keep this explicit revision non-destructive and adoptable.
    op.create_table(
        "canonical_runtime_safety_audits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("publication_id", sa.Integer(), nullable=True),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
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
            ["publication_id"],
            ["publications.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "source_fingerprint",
            name="uq_canonical_runtime_safety_audit_source",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_canonical_runtime_safety_audit_publication",
        "canonical_runtime_safety_audits",
        ["publication_id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    # Safety evidence is intentionally one-way. Refuse to discard a non-empty audit.
    connection = op.get_bind()
    count = connection.execute(
        sa.text("SELECT COUNT(*) FROM canonical_runtime_safety_audits")
    ).scalar_one()
    if int(count or 0) != 0:
        raise RuntimeError(
            "cannot downgrade canonical runtime safety audit while evidence exists"
        )
    op.drop_index(
        "ix_canonical_runtime_safety_audit_publication",
        table_name="canonical_runtime_safety_audits",
    )
    op.drop_table("canonical_runtime_safety_audits")
