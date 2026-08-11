from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)


def _settings(**overrides) -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(_env_file=None, **overrides)


def test_primary_delivery_is_disabled_by_default() -> None:
    config = _settings()

    assert config.enabled is False
    assert config.interval_seconds == 5
    assert config.batch_size == 25
    assert config.scan_limit == 500
    assert config.lease_ttl_seconds == 180
    assert config.heartbeat_interval_seconds == 30


def test_primary_delivery_accepts_safe_explicit_configuration() -> None:
    config = _settings(
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_INTERVAL_SECONDS=7,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_BATCH_SIZE=31,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_SCAN_LIMIT=411,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_LEASE_TTL_SECONDS=120,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_HEARTBEAT_INTERVAL_SECONDS=29,
    )

    assert config.enabled is True
    assert config.recovery_enabled is True
    assert config.interval_seconds == 7
    assert config.batch_size == 31
    assert config.scan_limit == 411
    assert config.lease_ttl_seconds == 120
    assert config.heartbeat_interval_seconds == 29


def test_primary_delivery_requires_recovery_when_enabled() -> None:
    with pytest.raises(ValidationError, match="requires canonical delivery recovery"):
        _settings(CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CANONICAL_PUBLICATION_DELIVERY_WORKER_INTERVAL_SECONDS", 0),
        ("CANONICAL_PUBLICATION_DELIVERY_WORKER_BATCH_SIZE", 0),
        ("CANONICAL_PUBLICATION_DELIVERY_WORKER_SCAN_LIMIT", 501),
        ("CANONICAL_PUBLICATION_DELIVERY_WORKER_LEASE_TTL_SECONDS", 29),
    ],
)
def test_primary_delivery_rejects_unsafe_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        _settings(
            CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
            CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
            **{field: value},
        )


def test_primary_delivery_heartbeat_must_precede_half_life() -> None:
    with pytest.raises(ValidationError, match="strictly less than half"):
        _settings(
            CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
            CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
            CANONICAL_PUBLICATION_DELIVERY_WORKER_LEASE_TTL_SECONDS=120,
            CANONICAL_PUBLICATION_DELIVERY_WORKER_HEARTBEAT_INTERVAL_SECONDS=60,
        )

    safe = _settings(
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_LEASE_TTL_SECONDS=120,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_HEARTBEAT_INTERVAL_SECONDS=59,
    )
    assert safe.heartbeat_interval_seconds == 59
