"""Add resumable admin-agent workflow state and artifact links.

Revision ID: 20260919_0019
Revises: 20260919_0018
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260919_0019"
down_revision = "20260919_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("admin_agent_runs", sa.Column("skill_id", sa.String(length=64), nullable=True))
    op.add_column("admin_agent_runs", sa.Column("skill_version", sa.String(length=32), nullable=True))
    op.add_column("admin_agent_runs", sa.Column("workflow_phase", sa.String(length=64), nullable=True))
    op.add_column("admin_agent_runs", sa.Column("checkpoint", sa.JSON(), nullable=True))
    op.add_column(
        "admin_agent_runs",
        sa.Column("execution_claim_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "admin_agent_runs",
        sa.Column("execution_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_admin_agent_runs_skill_version",
        "admin_agent_runs",
        ["skill_id", "skill_version"],
        unique=False,
    )

    op.create_table(
        "admin_agent_run_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content_item_id", sa.Integer(), nullable=False),
        sa.Column("content_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["admin_agent_runs.id"],
            name="fk_admin_agent_run_artifacts_run_id_admin_agent_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name="fk_admin_agent_run_artifacts_content_item_id_content_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "artifact_type",
            "ordinal",
            name="uq_admin_agent_run_artifact_ordinal",
        ),
        sa.UniqueConstraint(
            "run_id",
            "content_item_id",
            name="uq_admin_agent_run_artifact_content",
        ),
    )
    op.create_index(
        "ix_admin_agent_run_artifacts_run",
        "admin_agent_run_artifacts",
        ["run_id", "artifact_type", "ordinal"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_run_artifacts_run_id"),
        "admin_agent_run_artifacts",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_run_artifacts_content_item_id"),
        "admin_agent_run_artifacts",
        ["content_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_admin_agent_run_artifacts_content_item_id"),
        table_name="admin_agent_run_artifacts",
    )
    op.drop_index(
        op.f("ix_admin_agent_run_artifacts_run_id"),
        table_name="admin_agent_run_artifacts",
    )
    op.drop_index(
        "ix_admin_agent_run_artifacts_run",
        table_name="admin_agent_run_artifacts",
    )
    op.drop_table("admin_agent_run_artifacts")
    op.drop_index("ix_admin_agent_runs_skill_version", table_name="admin_agent_runs")
    op.drop_column("admin_agent_runs", "execution_claimed_at")
    op.drop_column("admin_agent_runs", "execution_claim_token")
    op.drop_column("admin_agent_runs", "checkpoint")
    op.drop_column("admin_agent_runs", "workflow_phase")
    op.drop_column("admin_agent_runs", "skill_version")
    op.drop_column("admin_agent_runs", "skill_id")
