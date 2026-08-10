from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.services.canonical_publication_edit import (
    CanonicalPublicationEditCoordinator,
    CanonicalPublicationEditSyncFailed,
)
from app.services.publication_editor import PublicationEditorView
from app.services.telegram_edit_outcome import TelegramEditOutcome


class DummySessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class DummySessionFactory:
    def __call__(self):
        return DummySessionContext()


class DummyProvider:
    async def edit_message_text(self, **kwargs):
        raise AssertionError("provider call not expected")

    async def edit_message_media(self, **kwargs):
        raise AssertionError("provider call not expected")


def test_unexpected_post_provider_error_is_safe_sync_failure(monkeypatch) -> None:
    async def run() -> None:
        from app.services import canonical_publication_edit as coordinator_module

        async def explode(self, **kwargs):
            raise RuntimeError("db-url-with-secret-like-detail")

        monkeypatch.setattr(
            coordinator_module.PublicationEditPersistenceService,
            "persist_success",
            explode,
        )

        coordinator = CanonicalPublicationEditCoordinator(
            provider=DummyProvider(),
            session_factory=DummySessionFactory(),  # type: ignore[arg-type]
        )
        view = PublicationEditorView(
            publication_id=9,
            content_item_id=10,
            content_revision=3,
            channel_id=11,
            channel_title="Owned",
            tg_chat_id=-10011,
            status="published",
            scheduled_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            timezone="UTC",
            repeat_rule={},
            publication_meta={},
            document={"schema_version": 1, "mode": "classic", "blocks": []},
            telegram_message_ids=(101,),
            result_link=None,
        )
        outcome = TelegramEditOutcome(message_id=101, attempted_message_ids=(101,))

        with pytest.raises(CanonicalPublicationEditSyncFailed) as captured:
            await coordinator._persist(  # noqa: SLF001 - boundary regression
                view=view,
                tg_user_id=123,
                expected_revision=3,
                payload={"type": "text", "text": "Edited"},
                outcome=outcome,
            )

        assert captured.value.conflict is False
        assert captured.value.error_type == "RuntimeError"
        assert str(captured.value) == "canonical edit sync failed"
        assert captured.value.__cause__ is None
        assert "secret" not in str(captured.value).lower()

    asyncio.run(run())
