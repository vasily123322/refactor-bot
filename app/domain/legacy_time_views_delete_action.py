from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LegacyTimeViewsDeleteAction(Base):
    __tablename__ = "legacy_time_views_delete_actions"
    __table_args__ = (
        UniqueConstraint(
            "post_task_id",
            name="uq_legacy_time_views_delete_action_post_task",
        ),
        CheckConstraint(
            "state IN ('reserved', 'succeeded', 'unknown')",
            name="ck_legacy_time_views_delete_action_state",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    post_task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(nullable=False)
    message_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reservation_token: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
