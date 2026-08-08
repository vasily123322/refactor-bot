from __future__ import annotations

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.domain.mixins import TimestampHelpersMixin


class AIAutoTask(TimestampHelpersMixin, Base):
    """Scheduled AI task configured for one channel and task type."""

    __tablename__ = "ai_auto_tasks"
    __table_args__ = (
        UniqueConstraint("channel_id", "task_type", name="uq_ai_auto_task_channel_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    run_at: Mapped[str] = mapped_column(String(5), default="10:00", nullable=False)
    schedule: Mapped[str] = mapped_column(String(16), default="daily", nullable=False)
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
