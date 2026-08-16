from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.bot.routers.content_plan_cancellation as bridge_module
from app.services.content_plan_history_identity import (
    LEGACY_POST_TASK_CALLBACK_ID_META_KEY,
    HistoryPublicationIdentity,
    HistoryPublicationIdentityKind,
    resolve_history_publication_identity,
)
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    INTENTIONAL_LEGACY_EXECUTION_MODE,
)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.rows)

    async def get(self, *_args, **_kwargs):
        raise AssertionError("canonical callback identity must not read PostTask")


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


def _row(
    publication_id: int,
    *,
    channel_id: int = 77,
    execution_mode: str | None = CANONICAL_EXECUTION_MODE,
    legacy_post_task_id: int | None = 41,
):
    return SimpleNamespace(
        id=publication_id,
        channel_id=channel_id,
        execution_mode=execution_mode,
        legacy_post_task_id=legacy_post_task_id,
    )


@pytest.mark.asyncio
async def test_durable_alias_resolves_canonical_identity_without_posttask_read():
    session = _Session([_row(501, legacy_post_task_id=None)])

    identity = await resolve_history_publication_identity(
        session,
        legacy_post_task_id=41,
    )

    assert identity == HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.CANONICAL_LINKED,
        publication_id=501,
        channel_id=77,
    )
    assert LEGACY_POST_TASK_CALLBACK_ID_META_KEY in str(session.statement)


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
async def test_intentional_legacy_link_including_time_views_stays_legacy():
    identity = await resolve_history_publication_identity(
        _Session(
            [
                _row(
                    502,
                    execution_mode=INTENTIONAL_LEGACY_EXECUTION_MODE,
                    legacy_post_task_id=42,
                )
            ]
        ),
        legacy_post_task_id=42,
    )

    assert identity == HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.LEGACY_ONLY
    )


@pytest.mark.asyncio
async def test_unknown_linked_execution_mode_fails_closed():
    identity = await resolve_history_publication_identity(
        _Session([_row(503, execution_mode=None, legacy_post_task_id=43)]),
        legacy_post_task_id=43,
    )

    assert identity == HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.FAIL_CLOSED
    )


@pytest.mark.asyncio
async def test_multiple_canonical_links_fail_closed():
    identity = await resolve_history_publication_identity(
        _Session([_row(601), _row(602)]),
        legacy_post_task_id=41,
    )

    assert identity == HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.FAIL_CLOSED
    )


@pytest.mark.asyncio
async def test_linked_history_open_uses_exact_publication_id(monkeypatch):
    callback = _Callback("cp_open_post:44:2026-08-16")
    canonical_calls = []
    legacy_calls = []

    async def _identity(post_id):
        assert post_id == 44
        return HistoryPublicationIdentity(
            HistoryPublicationIdentityKind.CANONICAL_LINKED,
            publication_id=704,
            channel_id=77,
        )

    async def _canonical(cb, state):
        canonical_calls.append((cb.data, state))

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(bridge_module, "_identity_for_post_id", _identity)
    monkeypatch.setattr(bridge_module, "cb_cp_open_publication", _canonical)
    monkeypatch.setattr(bridge_module, "cb_cp_open_post", _legacy)

    state = object()
    await bridge_module.cb_cp_open_post_history_bridge(callback, state)

    assert canonical_calls == [("cp_open_pub:704:2026-08-16", state)]
    assert legacy_calls == []


@pytest.mark.asyncio
async def test_linked_history_edit_uses_canonical_editor_without_posttask(monkeypatch):
    callback = _Callback("cp_edit_post:45:2026-08-16")
    canonical_calls = []
    legacy_calls = []

    async def _identity(post_id):
        assert post_id == 45
        return HistoryPublicationIdentity(
            HistoryPublicationIdentityKind.CANONICAL_LINKED,
            publication_id=705,
            channel_id=77,
        )

    async def _canonical(cb, state):
        canonical_calls.append((cb.data, state))

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(bridge_module, "_identity_for_post_id", _identity)
    monkeypatch.setattr(bridge_module, "cb_cp_edit_publication", _canonical)
    monkeypatch.setattr(bridge_module, "cb_cp_edit_post", _legacy)

    state = object()
    await bridge_module.cb_cp_edit_post_history_bridge(callback, state)

    assert canonical_calls == [("cp_edit_pub:705:2026-08-16", state)]
    assert legacy_calls == []


@pytest.mark.asyncio
async def test_linked_history_delete_routes_publication_cancellation(monkeypatch):
    callback = _Callback("cp_delete_post:46:2026-08-16")
    canonical_calls = []

    async def _identity(post_id):
        assert post_id == 46
        return HistoryPublicationIdentity(
            HistoryPublicationIdentityKind.CANONICAL_LINKED,
            publication_id=706,
            channel_id=77,
        )

    async def _canonical(cb, state):
        canonical_calls.append((cb.data, state))

    monkeypatch.setattr(bridge_module, "_identity_for_post_id", _identity)
    monkeypatch.setattr(bridge_module, "cb_cp_delete_publication", _canonical)

    state = object()
    await bridge_module.cb_cp_delete_post_canonical(callback, state)

    assert canonical_calls == [("cp_delete_pub:706:2026-08-16", state)]


@pytest.mark.asyncio
async def test_linked_repeat_off_fails_closed_without_legacy_mutation(monkeypatch):
    callback = _Callback("cp_repeat_off:47")
    legacy_calls = []

    async def _identity(post_id):
        assert post_id == 47
        return HistoryPublicationIdentity(
            HistoryPublicationIdentityKind.CANONICAL_LINKED,
            publication_id=707,
            channel_id=77,
        )

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(bridge_module, "_identity_for_post_id", _identity)
    monkeypatch.setattr(bridge_module, "cb_cp_repeat_off", _legacy)

    await bridge_module.cb_cp_repeat_off_history_bridge(callback, object())

    assert legacy_calls == []
    assert callback.answers == [
        (
            "Автоповтор canonical-публикации нельзя отключить через старую кнопку",
            {"show_alert": True},
        )
    ]


@pytest.mark.asyncio
async def test_true_legacy_history_keeps_existing_detail_handler(monkeypatch):
    callback = _Callback("cp_open_post:48:2026-08-16")
    canonical_calls = []
    legacy_calls = []

    async def _identity(post_id):
        assert post_id == 48
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.LEGACY_ONLY)

    async def _canonical(cb, state):
        canonical_calls.append((cb.data, state))

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(bridge_module, "_identity_for_post_id", _identity)
    monkeypatch.setattr(bridge_module, "cb_cp_open_publication", _canonical)
    monkeypatch.setattr(bridge_module, "cb_cp_open_post", _legacy)

    state = object()
    await bridge_module.cb_cp_open_post_history_bridge(callback, state)

    assert legacy_calls == [("cp_open_post:48:2026-08-16", state)]
    assert canonical_calls == []


@pytest.mark.asyncio
async def test_true_legacy_repeat_off_keeps_existing_posttask_handler(monkeypatch):
    callback = _Callback("cp_repeat_off:49")
    legacy_calls = []

    async def _identity(post_id):
        assert post_id == 49
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.LEGACY_ONLY)

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(bridge_module, "_identity_for_post_id", _identity)
    monkeypatch.setattr(bridge_module, "cb_cp_repeat_off", _legacy)

    state = object()
    await bridge_module.cb_cp_repeat_off_history_bridge(callback, state)

    assert legacy_calls == [("cp_repeat_off:49", state)]
    assert callback.answers == []


@pytest.mark.asyncio
async def test_ambiguous_link_never_falls_back_to_legacy(monkeypatch):
    callback = _Callback("cp_edit_post:50:2026-08-16")
    canonical_calls = []
    legacy_calls = []

    async def _identity(post_id):
        assert post_id == 50
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)

    async def _canonical(cb, state):
        canonical_calls.append((cb.data, state))

    async def _legacy(cb, state):
        legacy_calls.append((cb.data, state))

    monkeypatch.setattr(bridge_module, "_identity_for_post_id", _identity)
    monkeypatch.setattr(bridge_module, "cb_cp_edit_publication", _canonical)
    monkeypatch.setattr(bridge_module, "cb_cp_edit_post", _legacy)

    await bridge_module.cb_cp_edit_post_history_bridge(callback, object())

    assert canonical_calls == []
    assert legacy_calls == []
    assert callback.answers == [
        ("Публикацию нельзя однозначно определить", {"show_alert": True})
    ]
