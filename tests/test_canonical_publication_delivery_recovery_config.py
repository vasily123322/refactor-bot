from __future__ import annotations

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


def test_canonical_delivery_recovery_worker_is_disabled_by_default() -> None:
    config = _settings()

    assert config.canonical_publication_delivery_recovery_worker_enabled is False
    assert config.canonical_publication_delivery_recovery_worker_interval_seconds == 60
    assert config.canonical_publication_delivery_recovery_worker_batch_size == 100


def test_canonical_delivery_recovery_worker_accepts_explicit_env_aliases() -> None:
    config = _settings(
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_INTERVAL_SECONDS=75,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_BATCH_SIZE=37,
    )

    assert config.canonical_publication_delivery_recovery_worker_enabled is True
    assert config.canonical_publication_delivery_recovery_worker_interval_seconds == 75
    assert config.canonical_publication_delivery_recovery_worker_batch_size == 37
