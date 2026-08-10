from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PublicationAutodeleteLease(Base):
    """Durable ownership lease for one canonical Publication autodelete attempt."""

    __tablename__ = "publication_autodelete_leases"
    __table_args__ = (
        UniqueConstraint("lease_token", name="uq_publication_autodelete_lease_token"),
    )

    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"),
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
