from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from app.bot import dispatcher
from app.core.config import Settings
from app.core.runtime_configuration import (
    RuntimeConfigurationError,
    validate_runtime_configuration,
)


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


def test_default_runtime_configuration_is_safe() -> None:
    validate_runtime_configuration(_settings())


def test_successful_retention_requires_canonical_autodelete_worker() -> None:
    config = _settings(
        POST_TASK_RETENTION_ENABLED=True,
        POST_TASK_RETENTION_SUCCESSFUL_ENABLED=True,
        PUBLICATION_AUTODELETE_WORKER_ENABLED=False,
    )

    with pytest.raises(
        RuntimeConfigurationError,
        match="POST_TASK_RETENTION_SUCCESSFUL_ENABLED requires",
    ):
        validate_runtime_configuration(config)


def test_inactive_success_scope_does_not_require_executor() -> None:
    config = _settings(
        POST_TASK_RETENTION_ENABLED=False,
        POST_TASK_RETENTION_SUCCESSFUL_ENABLED=True,
        PUBLICATION_AUTODELETE_WORKER_ENABLED=False,
    )

    validate_runtime_configuration(config)


def test_successful_retention_is_safe_with_canonical_executor() -> None:
    config = _settings(
        POST_TASK_RETENTION_ENABLED=True,
        POST_TASK_RETENTION_SUCCESSFUL_ENABLED=True,
        PUBLICATION_AUTODELETE_WORKER_ENABLED=True,
    )

    validate_runtime_configuration(config)


def test_dispatcher_rejects_unsafe_runtime_before_database_bootstrap(monkeypatch) -> None:
    setup_logging = Mock()
    prepare_db_storage = Mock()

    def reject_runtime(_settings) -> None:
        raise RuntimeConfigurationError("unsafe runtime")

    monkeypatch.setattr(dispatcher, "setup_logging", setup_logging)
    monkeypatch.setattr(dispatcher, "validate_runtime_configuration", reject_runtime)
    monkeypatch.setattr(dispatcher, "prepare_db_storage_sync", prepare_db_storage)

    with pytest.raises(RuntimeConfigurationError, match="unsafe runtime"):
        asyncio.run(dispatcher.run_bot())

    setup_logging.assert_called_once()
    prepare_db_storage.assert_not_called()
