from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.config import Settings
from app.workers import post_task_retention as retention_worker_module
from app.workers.post_task_retention import PostTaskRetentionWorker


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


def test_successful_retention_is_separately_disabled_by_default() -> None:
    config = _settings(POST_TASK_RETENTION_ENABLED=True)

    assert config.post_task_retention_enabled is True
    assert config.post_task_retention_successful_enabled is False
    assert config.post_task_retention_successful_pending_autodelete_enabled is False
    assert config.post_task_retention_successful_repeat_occurrences_enabled is False


def test_successful_retention_accepts_explicit_env_aliases() -> None:
    config = _settings(
        POST_TASK_RETENTION_ENABLED=True,
        POST_TASK_RETENTION_SUCCESSFUL_ENABLED=True,
        POST_TASK_RETENTION_SUCCESSFUL_PENDING_AUTODELETE_ENABLED=True,
        POST_TASK_RETENTION_SUCCESSFUL_REPEAT_OCCURRENCES_ENABLED=True,
    )

    assert config.post_task_retention_enabled is True
    assert config.post_task_retention_successful_enabled is True
    assert config.post_task_retention_successful_pending_autodelete_enabled is True
    assert config.post_task_retention_successful_repeat_occurrences_enabled is True


def test_retention_worker_forwards_success_scopes_to_service(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}

        class SessionContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

        def session_factory():
            return SessionContext()

        class FakeService:
            def __init__(self, session, **kwargs) -> None:
                captured["session"] = session
                captured.update(kwargs)

            async def run_once(self):
                return SimpleNamespace(
                    deleted=0,
                    skipped_repeat=0,
                    skipped_delivery_evidence=0,
                    skipped_canonical_delivery=0,
                    skipped_pending_autodelete=0,
                    skipped_content_linkage=0,
                    failures=0,
                    selected=0,
                    eligible=0,
                    skipped_changed=0,
                )

        monkeypatch.setattr(
            retention_worker_module,
            "PostTaskRetentionService",
            FakeService,
        )
        worker = PostTaskRetentionWorker(
            session_factory=session_factory,  # type: ignore[arg-type]
            retention_days=123,
            batch_size=17,
            retire_successful=True,
            retire_successful_pending_autodelete=True,
            retire_successful_repeat_occurrences=True,
        )

        await worker._tick()  # noqa: SLF001 - worker/service wiring boundary

        assert captured["retention_days"] == 123
        assert captured["batch_size"] == 17
        assert captured["retire_successful"] is True
        assert captured["retire_successful_pending_autodelete"] is True
        assert captured["retire_successful_repeat_occurrences"] is True

    asyncio.run(run())
