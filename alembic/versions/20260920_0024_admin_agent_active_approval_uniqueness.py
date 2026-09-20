"""Enforce one active admin-agent approval per logical target.

Revision ID: 20260920_0024
Revises: 20260919_0023
Create Date: 2026-09-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260920_0024"
down_revision = "20260919_0023"
branch_labels = None
depends_on = None

_ACTIVE_PREDICATE = "state IN ('pending_review','executing')"


def _fail_on_duplicate_active_targets() -> None:
    bind = op.get_bind()

    duplicate_single = bind.execute(
        sa.text(
            """
            SELECT
                owner_tg_user_id,
                channel_id,
                action_type,
                content_item_id,
                content_revision,
                COUNT(*) AS duplicate_count
            FROM admin_agent_approvals
            WHERE state IN ('pending_review','executing')
            GROUP BY
                owner_tg_user_id,
                channel_id,
                action_type,
                content_item_id,
                content_revision
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).mappings().first()
    if duplicate_single is not None:
        raise RuntimeError(
            "duplicate active admin_agent_approvals target blocks migration: "
            f"owner={duplicate_single['owner_tg_user_id']} "
            f"channel={duplicate_single['channel_id']} "
            f"action={duplicate_single['action_type']} "
            f"content_item={duplicate_single['content_item_id']} "
            f"content_revision={duplicate_single['content_revision']} "
            f"count={duplicate_single['duplicate_count']}"
        )

    duplicate_series = bind.execute(
        sa.text(
            """
            SELECT
                owner_tg_user_id,
                channel_id,
                action_type,
                source_run_id,
                COUNT(*) AS duplicate_count
            FROM admin_agent_approval_batches
            WHERE state IN ('pending_review','executing')
            GROUP BY
                owner_tg_user_id,
                channel_id,
                action_type,
                source_run_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).mappings().first()
    if duplicate_series is not None:
        raise RuntimeError(
            "duplicate active admin_agent_approval_batches source run blocks migration: "
            f"owner={duplicate_series['owner_tg_user_id']} "
            f"channel={duplicate_series['channel_id']} "
            f"action={duplicate_series['action_type']} "
            f"source_run={duplicate_series['source_run_id']} "
            f"count={duplicate_series['duplicate_count']}"
        )


def upgrade() -> None:
    _fail_on_duplicate_active_targets()

    op.create_index(
        "uq_admin_agent_approval_active_target",
        "admin_agent_approvals",
        [
            "owner_tg_user_id",
            "channel_id",
            "action_type",
            "content_item_id",
            "content_revision",
        ],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_PREDICATE),
        postgresql_where=sa.text(_ACTIVE_PREDICATE),
    )
    op.create_index(
        "uq_admin_agent_approval_batch_active_source_run",
        "admin_agent_approval_batches",
        [
            "owner_tg_user_id",
            "channel_id",
            "action_type",
            "source_run_id",
        ],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_PREDICATE),
        postgresql_where=sa.text(_ACTIVE_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_admin_agent_approval_batch_active_source_run",
        table_name="admin_agent_approval_batches",
    )
    op.drop_index(
        "uq_admin_agent_approval_active_target",
        table_name="admin_agent_approvals",
    )
