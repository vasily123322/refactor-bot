from __future__ import annotations

from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)


class _UnusedSender:
    async def send_document(self, chat_id, document, *, asset_channel_id=None):
        raise AssertionError("configuration test must not call provider")


def test_executor_clamps_heartbeat_to_effective_lease_half_life() -> None:
    executor = CanonicalPublicationDeliveryExecutor(
        None,  # type: ignore[arg-type] - no DB work occurs in this constructor test
        sender=_UnusedSender(),
        lease_seconds=1,
        heartbeat_interval_seconds=120,
    )

    assert executor.lease_seconds == 30
    assert executor.heartbeat_interval_seconds == 15.0


def test_executor_clamps_oversized_lease_and_invalid_configuration() -> None:
    oversized = CanonicalPublicationDeliveryExecutor(
        None,  # type: ignore[arg-type]
        sender=_UnusedSender(),
        lease_seconds=999999,
        heartbeat_interval_seconds=999999,
    )
    assert oversized.lease_seconds == 3600
    assert oversized.heartbeat_interval_seconds == 120.0

    fallback = CanonicalPublicationDeliveryExecutor(
        None,  # type: ignore[arg-type]
        sender=_UnusedSender(),
        lease_seconds="invalid",  # type: ignore[arg-type]
        heartbeat_interval_seconds="invalid",  # type: ignore[arg-type]
    )
    assert fallback.lease_seconds == 180
    assert fallback.heartbeat_interval_seconds == 45.0
