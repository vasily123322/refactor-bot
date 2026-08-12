"""Merge durable delivery and time-autodelete action histories.

Revision ID: 20260812_0007
Revises: 20260811_0006, 20260812_0006
Create Date: 2026-08-12
"""

from __future__ import annotations

from typing import Sequence, Union


revision: str = "20260812_0007"
down_revision: Union[str, Sequence[str], None] = (
    "20260811_0006",
    "20260812_0006",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both parent revisions independently own their durable action tables. This merge
    # revision only converges Alembic history into one canonical head.
    pass


def downgrade() -> None:
    # Alembic splits back to both parents. Each parent migration owns the fail-closed
    # downgrade guard for its durable no-replay ledger.
    pass
