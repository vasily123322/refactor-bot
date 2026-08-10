from __future__ import annotations

import asyncio

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


def test_canonical_autodelete_worker_is_disabled_by_default() -> None:
    config = _settings()

    assert config.publication_autodelete_worker_enabled is False
    assert config.publication_autodelete_worker_interval_seconds == 60
    assert config.publication_autodelete_worker_batch_size == 25
    assert config.publication_autodelete_worker_lease_ttl_seconds == 180


def test_canonical_autodelete_worker_settings_accept_explicit_env_aliases() -> None:
    config = _settings(
        PUBLICATION_AUTODELETE_WORKER_ENABLED=True,
        PUBLICATION_AUTODELETE_WORKER_INTERVAL_SECONDS=75,
        PUBLICATION_AUTODELETE_WORKER_BATCH_SIZE=17,
        PUBLICATION_AUTODELETE_WORKER_LEASE_TTL_SECONDS=240,
    )

    assert config.publication_autodelete_worker_enabled is True
    assert config.publication_autodelete_worker_interval_seconds == 75
    assert config.publication_autodelete_worker_batch_size == 17
    assert config.publication_autodelete_worker_lease_ttl_seconds == 240


def test_dispatcher_start_helper_does_not_construct_worker_when_disabled(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        class UnexpectedWorker:
            def __init__(self, **kwargs) -> None:
                raise AssertionError("disabled worker must not be constructed")

        monkeypatch.setattr(dispatcher, "PublicationAutodeleteWorker", UnexpectedWorker)
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_worker_enabled",
            False,
        )

        worker = await dispatcher._start_publication_autodelete_worker_if_enabled()
        assert worker is None

    asyncio.run(run())


def test_dispatcher_start_helper_passes_explicit_worker_configuration(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        captured: dict[str, object] = {}

        class FakeWorker:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)
                self.started = False

            async def start(self) -> None:
                self.started = True

        monkeypatch.setattr(dispatcher, "PublicationAutodeleteWorker", FakeWorker)
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_worker_enabled",
            True,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_worker_interval_seconds",
            75,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_worker_batch_size",
            17,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_worker_lease_ttl_seconds",
            240,
        )

        worker = await dispatcher._start_publication_autodelete_worker_if_enabled()

        assert isinstance(worker, FakeWorker)
        assert worker.started is True
        assert captured == {
            "provider": dispatcher.bot,
            "session_factory": dispatcher.AsyncSessionLocal,
            "interval_seconds": 75,
            "batch_size": 17,
            "lease_ttl_seconds": 240,
        }

    asyncio.run(run())
