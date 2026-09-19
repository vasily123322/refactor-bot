"""Add bounded recurring Admin Agent automations.

Revision ID: 20260919_0022
Revises: 20260919_0021
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260919_0022"
down_revision = "20260919_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_agent_automations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("skill_id", sa.String(length=64), nullable=False),
        sa.Column("skill_version", sa.String(length=32), nullable=False),
        sa.Column("operator_input", sa.JSON(), nullable=False),
        sa.Column("cadence_kind", sa.String(length=16), nullable=False),
        sa.Column("local_time", sa.String(length=5), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("definition_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cadence_kind IN ('daily','weekly')",
            name="ck_admin_agent_automation_cadence_kind",
        ),
        sa.CheckConstraint(
            "(cadence_kind = 'daily' AND weekday IS NULL) OR "
            "(cadence_kind = 'weekly' AND weekday BETWEEN 0 AND 6)",
            name="ck_admin_agent_automation_weekday",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            name="fk_admin_agent_automations_channel_id_channels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_tg_user_id",
            "channel_id",
            "request_id",
            name="uq_admin_agent_automation_request",
        ),
    )
    op.create_index(
        "ix_admin_agent_automations_due",
        "admin_agent_automations",
        ["enabled", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_agent_automations_owner_channel",
        "admin_agent_automations",
        ["owner_tg_user_id", "channel_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_agent_automations_skill_version",
        "admin_agent_automations",
        ["skill_id", "skill_version"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_automations_owner_tg_user_id"),
        "admin_agent_automations",
        ["owner_tg_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_automations_channel_id"),
        "admin_agent_automations",
        ["channel_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_agent_automations_next_run_at"),
        "admin_agent_automations",
        ["next_run_at"],
        unique=False,
    )

    with op.batch_alter_table("admin_agent_runs") as batch:
        batch.add_column(
            sa.Column("automation_id", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_foreign_key(
            "fk_admin_agent_runs_automation_id_admin_agent_automations",
            "admin_agent_automations",
            ["automation_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_unique_constraint(
            "uq_admin_agent_run_automation_occurrence",
            ["automation_id", "scheduled_for"],
        )
        batch.create_index(
            op.f("ix_admin_agent_runs_automation_id"),
            ["automation_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("admin_agent_runs") as batch:
        batch.drop_index(op.f("ix_admin_agent_runs_automation_id"))
        batch.drop_constraint(
            "uq_admin_agent_run_automation_occurrence",
            type_="unique",
        )
        batch.drop_constraint(
            "fk_admin_agent_runs_automation_id_admin_agent_automations",
            type_="foreignkey",
        )
        batch.drop_column("scheduled_for")
        batch.drop_column("automation_id")

    op.drop_index(
        op.f("ix_admin_agent_automations_next_run_at"),
        table_name="admin_agent_automations",
    )
    op.drop_index(
        op.f("ix_admin_agent_automations_channel_id"),
        table_name="admin_agent_automations",
    )
    op.drop_index(
        op.f("ix_admin_agent_automations_owner_tg_user_id"),
        table_name="admin_agent_automations",
    )
    op.drop_index(
        "ix_admin_agent_automations_skill_version",
        table_name="admin_agent_automations",
    )
    op.drop_index(
        "ix_admin_agent_automations_owner_channel",
        table_name="admin_agent_automations",
    )
    op.drop_index(
        "ix_admin_agent_automations_due",
        table_name="admin_agent_automations",
    )
    op.drop_table("admin_agent_automations")
