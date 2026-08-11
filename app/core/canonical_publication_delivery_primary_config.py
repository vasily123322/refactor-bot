from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CanonicalPublicationDeliveryPrimarySettings(BaseSettings):
    """Fail-closed configuration for the opt-in canonical primary delivery worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(
        default=False,
        alias="CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED",
    )
    interval_seconds: int = Field(
        default=5,
        alias="CANONICAL_PUBLICATION_DELIVERY_WORKER_INTERVAL_SECONDS",
    )
    batch_size: int = Field(
        default=25,
        alias="CANONICAL_PUBLICATION_DELIVERY_WORKER_BATCH_SIZE",
    )
    scan_limit: int = Field(
        default=500,
        alias="CANONICAL_PUBLICATION_DELIVERY_WORKER_SCAN_LIMIT",
    )
    lease_ttl_seconds: int = Field(
        default=180,
        alias="CANONICAL_PUBLICATION_DELIVERY_WORKER_LEASE_TTL_SECONDS",
    )
    heartbeat_interval_seconds: int = Field(
        default=30,
        alias="CANONICAL_PUBLICATION_DELIVERY_WORKER_HEARTBEAT_INTERVAL_SECONDS",
    )
    recovery_enabled: bool = Field(
        default=False,
        alias="CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED",
    )

    @model_validator(mode="after")
    def _validate_enabled_runtime(self):
        if not self.enabled:
            return self

        if not self.recovery_enabled:
            raise ValueError(
                "canonical primary delivery requires canonical delivery recovery enabled"
            )

        if not 1 <= self.interval_seconds <= 3600:
            raise ValueError("canonical delivery interval must be between 1 and 3600")
        if not 1 <= self.batch_size <= 500:
            raise ValueError("canonical delivery batch size must be between 1 and 500")
        if not 1 <= self.scan_limit <= 500:
            raise ValueError("canonical delivery scan limit must be between 1 and 500")
        if not 30 <= self.lease_ttl_seconds <= 3600:
            raise ValueError("canonical delivery lease TTL must be between 30 and 3600")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("canonical delivery heartbeat interval must be positive")
        if self.heartbeat_interval_seconds * 2 >= self.lease_ttl_seconds:
            raise ValueError(
                "canonical delivery heartbeat interval must be strictly less than half the lease TTL"
            )

        return self


def load_canonical_publication_delivery_primary_settings(
    *,
    env_file: str | None = ".env",
) -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(_env_file=env_file)
