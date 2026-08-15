from __future__ import annotations

import inspect

import pytest

import app.services.content_plan_publication_cancellation as cancellation_module
from app.bot.routers import content_plan_publication as publication_router
from app.services.content_plan_cancellation import ContentPlanDeleteResult
from app.services.content_plan_publication_cancellation import (
    ContentPlanPublicationCancellationService,
)
from app.services.content_plan_publication_links import (
    content_plan_open_callback,
    published_publication_ids_for_legacy_tasks,
)


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def one_or_none(self):
        if not self._rows:
            return None
        assert len(self._rows) == 1
        return self._rows[0]


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _RowsResult(self._rows)


class _Factory:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        session = _Session(self.rows)

        class _Context:
            async def __aenter__(self):
                return session

            async def __aexit__(self, _exc_type, _exc, _tb):
                return False

        return _Context()


@pytest.mark.asyncio
async def test_linked_content_plan_row_promotes_to_publication_callback():
    links = await published_publication_ids_for_legacy_tasks(
        _Session([(41, 91)]),
        channel_id=7,
        post_task_ids=[41],
    )
    assert links == {41: 91}
    assert (
        content_plan_open_callback(
            post_task_id=41,
            date_iso="2026-08-15",
            published_publication_ids=links,
        )
        == "cp_open_pub:91:2026-08-15"
    )


@pytest.mark.asyncio
async def test_publication_cancel_adapter_hides_post_task_identity(monkeypatch):
    calls = []

    class _CancellationService:
        def __init__(self, _factory):
            pass

        async def delete(self, post_task_id):
            calls.append(post_task_id)
            return ContentPlanDeleteResult(outcome="cancelled")

    monkeypatch.setattr(
        cancellation_module,
        "ContentPlanCancellationService",
        _CancellationService,
    )
    result = await ContentPlanPublicationCancellationService(
        _Factory([("queued", 41)])
    ).delete(91)
    assert result.outcome == "cancelled"
    assert calls == [41]


@pytest.mark.asyncio
async def test_publication_cancel_without_compatibility_transport_fails_closed():
    result = await ContentPlanPublicationCancellationService(
        _Factory([("queued", None)])
    ).delete(91)
    assert result.outcome == "cannot_cancel"
    assert result.reason == "compatibility_transport_absent"


def test_queued_publication_card_exposes_publication_id_delete_callback():
    open_source = inspect.getsource(publication_router.cb_cp_open_publication)
    delete_source = inspect.getsource(publication_router.cb_cp_delete_publication)
    assert 'view.status == "queued"' in open_source
    assert 'callback_data=f"cp_delete_pub:{view.publication_id}:{date_iso}"' in open_source
    assert 'ContentPlanPublicationCancellationService' in delete_source
    assert 'load_owned_publication_editor_view' in delete_source
