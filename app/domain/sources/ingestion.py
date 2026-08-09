from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SourceIngestionLease(Base):
    __tablename__ = "source_ingestion_leases"

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("source_connectors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    holder: Mapped[str] = mapped_column(String(32), nullable=False)
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
