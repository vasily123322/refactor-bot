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


def test_views_autodelete_worker_is_disabled_by_default() -> None:
    config = _settings()

    assert config.publication_autodelete_views_worker_enabled is False
    assert config.publication_autodelete_views_worker_interval_seconds == 60
    assert config.publication_autodelete_views_worker_batch_size == 25
    assert config.publication_autodelete_views_worker_lease_ttl_seconds == 180
    assert config.publication_autodelete_views_worker_next_check_seconds == 60
    assert config.publication_autodelete_views_worker_ineligible_backoff_seconds == 300


def test_views_autodelete_worker_settings_accept_explicit_env_aliases() -> None:
    config = _settings(
        PUBLICATION_AUTODELETE_VIEWS_WORKER_ENABLED=True,
        PUBLICATION_AUTODELETE_VIEWS_WORKER_INTERVAL_SECONDS=75,
        PUBLICATION_AUTODELETE_VIEWS_WORKER_BATCH_SIZE=17,
        PUBLICATION_AUTODELETE_VIEWS_WORKER_LEASE_TTL_SECONDS=240,
        PUBLICATION_AUTODELETE_VIEWS_WORKER_NEXT_CHECK_SECONDS=90,
        PUBLICATION_AUTODELETE_VIEWS_WORKER_INELIGIBLE_BACKOFF_SECONDS=420,
    )

    assert config.publication_autodelete_views_worker_enabled is True
    assert config.publication_autodelete_views_worker_interval_seconds == 75
    assert config.publication_autodelete_views_worker_batch_size == 17
    assert config.publication_autodelete_views_worker_lease_ttl_seconds == 240
    assert config.publication_autodelete_views_worker_next_check_seconds == 90
    assert config.publication_autodelete_views_worker_ineligible_backoff_seconds == 420


def test_views_start_helper_does_not_construct_worker_when_disabled(monkeypatch) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        class UnexpectedWorker:
            def __init__(self, **kwargs) -> None:
                raise AssertionError("disabled worker must not be constructed")

        monkeypatch.setattr(
            dispatcher,
            "PublicationAutodeleteViewsWorker",
            UnexpectedWorker,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            False,
        )

        worker = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
        )
        assert worker is None

    asyncio.run(run())


def test_views_start_helper_fails_closed_when_userbot_is_unavailable(monkeypatch) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        class UnexpectedWorker:
            def __init__(self, **kwargs) -> None:
                raise AssertionError("worker must not start without userbot")

        monkeypatch.setattr(
            dispatcher,
            "PublicationAutodeleteViewsWorker",
            UnexpectedWorker,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )

        worker = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=False,
        )
        assert worker is None

    asyncio.run(run())


def test_views_start_helper_passes_explicit_worker_configuration(monkeypatch) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        captured: dict[str, object] = {}

        class FakeWorker:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)
                self.started = False

            async def start(self) -> None:
                self.started = True

        monkeypatch.setattr(dispatcher, "PublicationAutodeleteViewsWorker", FakeWorker)
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_interval_seconds",
            75,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_batch_size",
            17,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_lease_ttl_seconds",
            240,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_next_check_seconds",
            90,
        )
        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_ineligible_backoff_seconds",
            420,
        )

        worker = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
        )

        assert isinstance(worker, FakeWorker)
        assert worker.started is True
        assert captured == {
            "view_source": dispatcher.userbot,
            "delete_provider": dispatcher.bot,
            "session_factory": dispatcher.AsyncSessionLocal,
            "interval_seconds": 75,
            "batch_size": 17,
            "lease_ttl_seconds": 240,
            "next_check_seconds": 90,
            "ineligible_backoff_seconds": 420,
        }

    asyncio.run(run())
