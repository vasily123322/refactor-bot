from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
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


class PublicationAutodeleteAction(Base):
    """One-way destructive authority for one Telegram message deletion.

    A row is created and committed before Telegram is called. ``reserved`` and
    ``unknown`` are permanent no-replay barriers; only ``succeeded`` and
    ``unavailable`` are terminal evidence that lets the publication-level runtime
    finalize once every source message is resolved.
    """

    __tablename__ = "publication_autodelete_actions"
    __table_args__ = (
        UniqueConstraint(
            "reservation_token",
            name="uq_publication_autodelete_action_reservation_token",
        ),
        CheckConstraint(
            "state IN ('reserved', 'succeeded', 'unavailable', 'unknown')",
            name="ck_publication_autodelete_actions_state",
        ),
    )

    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"),
        primary_key=True,
    )
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authority_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reservation_token: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_by_lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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


class PublicationAutodeleteViewState(Base):
    """Indexed scheduler state for an active views-based autodelete intent.

    This row is intentionally not the terminal deletion truth. Successful deletion is
    recorded only in the Publication canonical runtime metadata used by retention.
    """

    __tablename__ = "publication_autodelete_view_states"
    __table_args__ = (
        CheckConstraint(
            "threshold > 0",
            name="ck_publication_autodelete_view_states_threshold_positive",
        ),
        CheckConstraint(
            "last_views IS NULL OR last_views >= 0",
            name="ck_publication_autodelete_view_states_last_views_nonnegative",
        ),
    )

    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"),
        primary_key=True,
    )
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    last_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_check_at: Mapped[datetime] = mapped_column(
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
