"""Adopt the 2026-08-09 ORM schema as the Alembic baseline.

Revision ID: 20260809_0001
Revises: None
Create Date: 2026-08-09
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

import app.domain  # noqa: F401 register ORM tables in Base.metadata
from app.core.db import Base


revision: str = "20260809_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Freeze the historical baseline membership. Never replace this with an unfiltered
# Base.metadata.create_all(): future ORM tables must be introduced by later Alembic
# revisions, otherwise a fresh database would create them during 0001 and collide
# with the explicit revision that owns them.
_BASELINE_TABLE_NAMES = frozenset(
    {
        "clients",
        "channels",
        "channel_settings",
        "grab_sources",
        "applications",
        "subscribers",
        "post_tasks",
        "ai_presets",
        "channel_ai_settings",
        "ai_sources",
        "ai_conversations",
        "ai_conversation_messages",
        "ai_custom_system_prompts",
        "external_bots",
        "mod_logs",
        "channel_bots",
        "join_requests",
        "admin_config",
        "banned_chats",
        "ai_auto_tasks",
        "content_items",
        "content_revisions",
        "media_assets",
        "schedule_entries",
        "publications",
        "publication_attempts",
        "source_connectors",
        "source_documents",
        "content_candidates",
        "candidate_enrichment_runs",
        "candidate_rewrite_runs",
        "source_ingestion_leases",
    }
)


def upgrade() -> None:
    bind = op.get_bind()
    known_names = set(Base.metadata.tables)
    missing_from_registry = _BASELINE_TABLE_NAMES - known_names
    if missing_from_registry:
        raise RuntimeError(
            "Alembic baseline ORM registry is incomplete: "
            + ", ".join(sorted(missing_from_registry))
        )

    # SQLAlchemy's sorted_tables respects FK dependencies. checkfirst=True keeps
    # adoption non-destructive for existing current databases while still creating
    # the complete frozen baseline on an empty database.
    for table in Base.metadata.sorted_tables:
        if table.name in _BASELINE_TABLE_NAMES:
            table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Never drop a pre-existing production schema when crossing the adoption marker.
    # Future revisions may provide reversible downgrades for their own changes.
    pass
