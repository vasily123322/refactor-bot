"""Adopt the current ORM schema as the Alembic baseline.

Revision ID: 20260809_0001
Revises: None
Create Date: 2026-08-09
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

import app.domain  # noqa: F401 register current ORM tables
from app.core.db import Base


revision: str = "20260809_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Non-destructive adoption strategy:
    # - empty database: create the complete current schema;
    # - existing current database: create only missing tables;
    # - future structural changes: explicit Alembic revisions from this baseline.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    # Never drop a pre-existing production schema when crossing the adoption marker.
    # Future revisions may provide reversible downgrades for their own changes.
    pass
