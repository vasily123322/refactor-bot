from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SchedulerTaskLease(Base):
    """Durable liveness lease for one processing PostTask."""

    __tablename__ = "scheduler_task_leases"
    __table_args__ = (
        UniqueConstraint("lease_token", name="uq_scheduler_task_lease_token"),
    )

    task_id: Mapped[int] = mapped_column(
        ForeignKey("post_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    holder: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
