from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class StudioChannelOnboardingRequest(Base):
    __tablename__ = "studio_channel_onboarding_requests"
    __table_args__ = (
        UniqueConstraint(
            "request_id",
            name="uq_studio_channel_onboarding_request_id",
        ),
        UniqueConstraint(
            "prepared_button_id",
            name="uq_studio_channel_onboarding_prepared_button_id",
        ),
        Index(
            "ix_studio_channel_onboarding_requests_request_id",
            "request_id",
        ),
        Index(
            "ix_studio_channel_onboarding_requests_client_id",
            "client_id",
        ),
        Index(
            "ix_studio_channel_onboarding_requests_expected_tg_user_id",
            "expected_tg_user_id",
        ),
        Index(
            "ix_studio_channel_onboarding_requests_status",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(Integer, nullable=False)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    expected_tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prepared_button_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24), default="reserved", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    selected_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL"),
        nullable=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
