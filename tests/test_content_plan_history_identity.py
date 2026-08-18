from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.bot.routers.content_plan_cancellation as bridge_module
from app.services.content_plan_history_identity import (
    HistoryPublicationIdentity,
    HistoryPublicationIdentityKind,
    resolve_history_publication_identity,
)


class _Scalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _Result:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _Scalars(self._values)


class _Session:
    def __init__(self, publication_ids):
        self.publication_ids = list(publication_ids)

    async def execute(self, _statement):
        return _Result(self.publication_ids)


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Callback:
    def __init__(self, data: str):
        self.data = data
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    def model_copy(self, *, update):
        return _Callback(update["data"])


@pytest.mark.asyncio
async def test_unique_link_resolves_publication_identity_without_posttask_read():
    identity = await resolve_history_publication_identity(
        _Session([501]),
        legacy_post_task_id=41,
    )

    assert identity == HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.CANONICAL_LINKED,
        publication_id=501,
    )


@pytest.mark.asyncio
async def test_no_publication_link_is_explicit_legacy_history_fallback():
    identity = await resolve_history_publication_identity(
        _Session([]),
        legacy_post_task_id=42,
    )

    assert identity == HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.LEGACY_ONLY
    )


@pytest.mark.asyncio
async def test_multiple_publication_links_fail_closed():
    identity = await resolve_history_publication_identity(
        _Session([601, 602]),
        legacy_post_task_id=43,
    )

    assert identity == HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.FAIL_CLOSED
    )


@pytest.mark.asyncio
async def test_linked_history_callback_uses_exact_publication_id(monkeypatch):
    callback = _Callback("cp_open_post:44:2026-08-16")
    canonical_calls = []
    legacy_calls = []

    async def _resolve(_session, *, legacy_post_task_id):
        assert legacy_post_task_id == 44
        return HistoryPublicationIdentity(
            HistoryPublicationIdentityKind.CANONICAL_LINKED,
            publication_id=704,
        )

    async def _canonical(cb, state):
        canonical_calls.append((cb.data, state))

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(
        bridge_module,
        "AsyncSessionLocal",
        lambda: _SessionContext(SimpleNamespace()),
    )
    monkeypatch.setattr(bridge_module, "resolve_history_publication_identity", _resolve)
    monkeypatch.setattr(bridge_module, "cb_cp_open_publication", _canonical)
    monkeypatch.setattr(bridge_module, "cb_cp_open_post", _legacy)

    state = object()
    await bridge_module.cb_cp_open_post_history_bridge(callback, state)

    assert canonical_calls == [("cp_open_pub:704:2026-08-16", state)]
    assert legacy_calls == []


@pytest.mark.asyncio
async def test_true_legacy_history_keeps_existing_detail_handler(monkeypatch):
    callback = _Callback("cp_open_post:45:2026-08-16")
    canonical_calls = []
    legacy_calls = []

    async def _resolve(_session, *, legacy_post_task_id):
        assert legacy_post_task_id == 45
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.LEGACY_ONLY)

    async def _canonical(cb, state):
        canonical_calls.append((cb.data, state))

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(
        bridge_module,
        "AsyncSessionLocal",
        lambda: _SessionContext(SimpleNamespace()),
    )
    monkeypatch.setattr(bridge_module, "resolve_history_publication_identity", _resolve)
    monkeypatch.setattr(bridge_module, "cb_cp_open_publication", _canonical)
    monkeypatch.setattr(bridge_module, "cb_cp_open_post", _legacy)

    state = object()
    await bridge_module.cb_cp_open_post_history_bridge(callback, state)

    assert legacy_calls == [("cp_open_post:45:2026-08-16", state)]
    assert canonical_calls == []


@pytest.mark.asyncio
async def test_ambiguous_link_never_falls_back_to_legacy(monkeypatch):
    callback = _Callback("cp_open_post:46:2026-08-16")
    canonical_calls = []
    legacy_calls = []

    async def _resolve(_session, *, legacy_post_task_id):
        assert legacy_post_task_id == 46
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)

    async def _canonical(cb, state):
        canonical_calls.append((cb.data, state))

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(
        bridge_module,
        "AsyncSessionLocal",
        lambda: _SessionContext(SimpleNamespace()),
    )
    monkeypatch.setattr(bridge_module, "resolve_history_publication_identity", _resolve)
    monkeypatch.setattr(bridge_module, "cb_cp_open_publication", _canonical)
    monkeypatch.setattr(bridge_module, "cb_cp_open_post", _legacy)

    await bridge_module.cb_cp_open_post_history_bridge(callback, object())

    assert canonical_calls == []
    assert legacy_calls == []
    assert callback.answers == [
        ("Публикацию нельзя однозначно определить", {"show_alert": True})
    ]
