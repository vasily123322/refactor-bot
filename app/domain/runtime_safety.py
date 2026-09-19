from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CanonicalRuntimeSafetyAudit(Base):
    """Durable fail-close evidence that survives retired runtime/schema removal."""

    __tablename__ = "canonical_runtime_safety_audits"
    __table_args__ = (
        UniqueConstraint(
            "source_fingerprint",
            name="uq_canonical_runtime_safety_audit_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    publication_id: Mapped[int | None] = mapped_column(
        ForeignKey("publications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
