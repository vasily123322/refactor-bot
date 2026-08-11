from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PublicationDeliveryLease(Base):
    """Durable execution liveness lease for one canonical Publication delivery."""

    __tablename__ = "publication_delivery_leases"
    __table_args__ = (
        UniqueConstraint("lease_token", name="uq_publication_delivery_lease_token"),
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


class PublicationDeliveryAction(Base):
    """Durable at-most-once reservation for one non-idempotent delivery auxiliary.

    A row is inserted before the provider call. Once the deterministic action key has
    been reserved it is never made claimable again, including after cancellation or a
    crash. This intentionally prefers a possibly-missed auxiliary effect over duplicate
    forwards or other repeated non-idempotent provider side effects.
    """

    __tablename__ = "publication_delivery_actions"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('pin', 'forward')",
            name="ck_publication_delivery_action_type",
        ),
        CheckConstraint(
            "state IN ('reserved', 'succeeded', 'unknown')",
            name="ck_publication_delivery_action_state",
        ),
    )

    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"),
        primary_key=True,
    )
    action_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    action_type: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    intent_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_by_lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
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
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
