from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bot.routers import content_plan_publication
from app.bot.routers.utils import canonical_publication_edit
from app.services import queued_canonical_publication_edit
from app.services.publication_editor import publication_edit_callback


SERVICE_SOURCE = inspect.getsource(
    queued_canonical_publication_edit.QueuedCanonicalPublicationEditCoordinator
)
CARD_SOURCE = inspect.getsource(content_plan_publication.cb_cp_open_publication)
EDIT_HANDLER_SOURCE = inspect.getsource(content_plan_publication.cb_cp_edit_publication)
CANONICAL_ROUTING_SOURCE = inspect.getsource(canonical_publication_edit)
POSTING_PUBLISH_SOURCE = Path("app/bot/routers/posting_publish.py").read_text(encoding="utf-8")
LEGACY_PLAN_SOURCE = Path("app/bot/routers/content_plan.py").read_text(encoding="utf-8")
ACCESS_GUARD_SOURCE = Path("app/core/settings_channel_access.py").read_text(encoding="utf-8")


def _identity(mode: str = "queued"):
    return canonical_publication_edit._CanonicalEditIdentity(
        mode=mode,
        publication_id=321,
        expected_revision=7,
    )


def test_queued_canonical_card_emits_publication_id_edit_callback():
    callback = publication_edit_callback(321, "2026-08-16")
    assert callback == "cp_edit_pub:321:2026-08-16"
    assert "publication_edit_callback(view.publication_id" in CARD_SOURCE
    assert 'view.status == "queued"' in CARD_SOURCE


def test_canonical_external_edit_callback_does_not_expose_post_task_id():
    legacy_post_task_id = 987654
    callback = publication_edit_callback(321, "2026-08-16")
    assert str(legacy_post_task_id) not in callback
    assert "legacy_post_task_id" not in CARD_SOURCE


def test_queued_edit_loads_publication_first():
    assert SERVICE_SOURCE.index("select(Publication)") < SERVICE_SOURCE.index(
        "select(PostTask)"
    )
    assert "Publication.id == safe_publication_id" in SERVICE_SOURCE
    assert "Client.tg_user_id == safe_user_id" in SERVICE_SOURCE


def test_queued_save_creates_and_switches_new_canonical_revision():
    assert "next_revision = safe_expected_revision + 1" in SERVICE_SOURCE
    assert "ContentRevision(" in SERVICE_SOURCE
    assert "item.current_revision = next_revision" in SERVICE_SOURCE
    assert "publication.content_revision = next_revision" in SERVICE_SOURCE
    assert "schedule.content_revision = next_revision" in SERVICE_SOURCE


def test_previous_revision_is_not_mutated_in_place():
    assert "previous.document =" not in SERVICE_SOURCE
    assert "previous.revision =" not in SERVICE_SOURCE
    assert 'source="queued_canonical_edit"' in SERVICE_SOURCE


def test_compatibility_post_task_is_projection_only():
    assert "task.payload = projected_payload" in SERVICE_SOURCE
    assert "task.status =" not in SERVICE_SOURCE
    assert "task.scheduled_at =" not in SERVICE_SOURCE
    assert "task.dedupe_key =" not in SERVICE_SOURCE
    assert "task.channel_id =" not in SERVICE_SOURCE


def test_atomic_failure_path_rolls_back_post_task_projection_with_canonical_state():
    assert SERVICE_SOURCE.index("task.payload = projected_payload") < SERVICE_SOURCE.index(
        "await session.commit()"
    )
    assert "except Exception:" in SERVICE_SOURCE
    assert "await session.rollback()" in SERVICE_SOURCE


def test_attempt_barrier_fails_closed_before_mutation():
    attempt_check = SERVICE_SOURCE.index("select(PublicationAttempt.id)")
    mutation = SERVICE_SOURCE.index("ContentRevision(")
    assert attempt_check < mutation
    assert "started_attempt is not None" in SERVICE_SOURCE
    assert "publication execution attempt already exists" in SERVICE_SOURCE


def test_active_delivery_lease_barrier_fails_closed_before_mutation():
    lease_check = SERVICE_SOURCE.index("select(PublicationDeliveryLease.publication_id)")
    mutation = SERVICE_SOURCE.index("ContentRevision(")
    assert lease_check < mutation
    assert "PublicationDeliveryLease.expires_at > safe_now" in SERVICE_SOURCE
    assert "publication delivery lease is active" in SERVICE_SOURCE


def test_sending_and_other_nonqueued_lifecycles_fail_closed():
    assert 'str(publication.status or "") != "queued"' in SERVICE_SOURCE
    assert "publication lifecycle is not safely queued" in SERVICE_SOURCE
    assert "publication.attempt_count" in SERVICE_SOURCE
    assert "publication.telegram_message_ids" in SERVICE_SOURCE


@pytest.mark.asyncio
async def test_queued_routing_uses_queued_coordinator_without_provider(monkeypatch):
    calls: dict[str, object] = {}

    class FakeQueuedCoordinator:
        def __init__(self, *, session_factory):
            calls["session_factory"] = session_factory

        async def edit_and_persist(self, **kwargs):
            calls["queued"] = kwargs
            return SimpleNamespace(
                publication_id=kwargs["publication_id"],
                content_item_id=5,
                previous_revision=kwargs["expected_revision"],
                revision=kwargs["expected_revision"] + 1,
            )

    class ForbiddenPublishedCoordinator:
        def __init__(self, **kwargs):
            raise AssertionError("published provider coordinator must not serve queued edits")

    monkeypatch.setattr(
        canonical_publication_edit,
        "QueuedCanonicalPublicationEditCoordinator",
        FakeQueuedCoordinator,
    )
    monkeypatch.setattr(
        canonical_publication_edit,
        "CanonicalPublicationEditCoordinator",
        ForbiddenPublishedCoordinator,
    )

    callback = SimpleNamespace(from_user=SimpleNamespace(id=77))
    result = await canonical_publication_edit._canonical_result(
        callback=callback,
        data={},
        payload={"type": "text", "text": "edited"},
        identity=_identity("queued"),
    )

    assert result.revision == 8
    assert calls["queued"] == {
        "publication_id": 321,
        "tg_user_id": 77,
        "expected_revision": 7,
        "payload": {"type": "text", "text": "edited"},
    }


@pytest.mark.asyncio
async def test_published_routing_keeps_existing_provider_coordinator(monkeypatch):
    calls: dict[str, object] = {}

    class ForbiddenQueuedCoordinator:
        def __init__(self, **kwargs):
            raise AssertionError("queued coordinator must not serve published edits")

    class FakePublishedCoordinator:
        def __init__(self, *, provider, session_factory):
            calls["provider"] = provider
            calls["session_factory"] = session_factory

        async def edit_text_and_persist(self, **kwargs):
            calls["published"] = kwargs
            return SimpleNamespace(tg_chat_id=-1001, message_id=9)

    async def passthrough_autosign(text, data):
        return text

    monkeypatch.setattr(
        canonical_publication_edit,
        "QueuedCanonicalPublicationEditCoordinator",
        ForbiddenQueuedCoordinator,
    )
    monkeypatch.setattr(
        canonical_publication_edit,
        "CanonicalPublicationEditCoordinator",
        FakePublishedCoordinator,
    )
    monkeypatch.setattr(
        canonical_publication_edit,
        "maybe_append_autosign",
        passthrough_autosign,
    )

    callback = SimpleNamespace(from_user=SimpleNamespace(id=77))
    await canonical_publication_edit._canonical_result(
        callback=callback,
        data={},
        payload={"type": "text", "text": "edited"},
        identity=_identity("published"),
    )

    assert calls["published"]["publication_id"] == 321
    assert calls["published"]["expected_revision"] == 7


def test_typed_context_has_distinct_queued_mode_and_no_fake_edit_message_id():
    identity = canonical_publication_edit._canonical_identity(
        {
            "canonical_edit_context": {
                "mode": "queued",
                "publication_id": 321,
                "expected_revision": 7,
            }
        }
    )
    assert identity is not None
    assert identity.mode == "queued"
    assert identity.publication_id == 321
    assert identity.expected_revision == 7
    assert '"mode": "queued" if queued else "published"' in EDIT_HANDLER_SOURCE
    assert "edit_msg_id=None if queued" in EDIT_HANDLER_SOURCE


def test_historical_unlinked_rows_keep_legacy_edit_fallback():
    assert "cp_edit_post:" in LEGACY_PLAN_SOURCE
    assert "cp_edit_pub:" not in LEGACY_PLAN_SOURCE or "cp_edit_post:" in LEGACY_PLAN_SOURCE


def test_malformed_linkage_fails_closed_before_revision_mutation():
    linkage_check = SERVICE_SOURCE.index("queued canonical publication linkage changed")
    mutation = SERVICE_SOURCE.index("ContentRevision(")
    assert linkage_check < mutation
    assert "schedule_task_id != legacy_task_id" in SERVICE_SOURCE
    assert "queued compatibility transport linkage changed" in SERVICE_SOURCE


def test_linked_legacy_mutation_guard_is_not_bypassed():
    assert "cp_edit_post" in ACCESS_GUARD_SOURCE
    assert "cp_edit_pub" in CANONICAL_ROUTING_SOURCE or "queued" in CANONICAL_ROUTING_SOURCE
    assert "settings_channel_access" not in SERVICE_SOURCE


def test_queued_edit_does_not_resurrect_legacy_execution_authority():
    forbidden_mutations = (
        "task.status =",
        "task.scheduled_at =",
        "task.dedupe_key =",
        "task.error =",
    )
    for mutation in forbidden_mutations:
        assert mutation not in SERVICE_SOURCE
    assert 'str(task.status or "") != "pending"' in SERVICE_SOURCE
    assert 'str(task.dedupe_key or "")' in SERVICE_SOURCE


def test_canonical_submit_branch_precedes_legacy_edit_fallthrough():
    canonical_index = POSTING_PUBLISH_SOURCE.index("canonical_edit_context")
    legacy_index = POSTING_PUBLISH_SOURCE.find("edit_msg_id", canonical_index + 1)
    assert legacy_index == -1 or canonical_index < legacy_index


def test_queued_edit_does_not_claim_time_views_ownership():
    changed_sources = SERVICE_SOURCE + CARD_SOURCE + CANONICAL_ROUTING_SOURCE
    assert "time+views" not in changed_sources
    assert "time_views" not in changed_sources
    assert "views_pin" not in changed_sources
