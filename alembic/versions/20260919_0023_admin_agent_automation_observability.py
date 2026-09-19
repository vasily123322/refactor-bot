"""Add Admin Agent automation operational observability metadata.

Revision ID: 20260919_0023
Revises: 20260919_0022
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260919_0023"
down_revision = "20260919_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("admin_agent_automations") as batch:
        batch.add_column(sa.Column("disabled_reason", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_outcome", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("last_outcome_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_admin_agent_automation_disabled_reason",
            "disabled_reason IS NULL OR disabled_reason IN "
            "('manual_pause','ownership_lost','unsupported_skill_version','invalid_definition')",
        )
        batch.create_check_constraint(
            "ck_admin_agent_automation_last_outcome",
            "last_outcome IS NULL OR last_outcome IN "
            "('run_recorded','misfire_skipped','safety_disabled')",
        )


def downgrade() -> None:
    with op.batch_alter_table("admin_agent_automations") as batch:
        batch.drop_constraint(
            "ck_admin_agent_automation_last_outcome",
            type_="check",
        )
        batch.drop_constraint(
            "ck_admin_agent_automation_disabled_reason",
            type_="check",
        )
        batch.drop_column("last_outcome_at")
        batch.drop_column("last_outcome")
        batch.drop_column("disabled_at")
        batch.drop_column("disabled_reason")
