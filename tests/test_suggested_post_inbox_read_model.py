from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api.studio.sources import _candidate_response
from app.api.studio.suggested_post_read_model import project_suggested_post_inbox
from app.domain.sources.models import ContentCandidate, SourceDocument


def _base_meta() -> dict:
    return {
        "transport": "telegram_suggested_posts",
        "telegram_direct_messages_chat_id": -1009001,
        "telegram_message_id": 77,
        "telegram_direct_messages_topic": {
            "topic_id": 123,
            "user": {
                "id": 501,
                "username": "alice",
                "first_name": "Alice",
            },
        },
        "telegram_sender": {
            "user": {
                "id": 501,
                "username": "alice",
                "first_name": "Alice",
            }
        },
        "telegram_suggested_post_info": {
            "state": "pending",
            "send_date": 1787184000,
        },
    }


def _document(meta: dict) -> SourceDocument:
    return SourceDocument(
        id=44,
        connector_id=9,
        channel_id=7,
        external_id="dm:-1009001:77",
        content="Current reconciled proposal",
        content_hash="a" * 64,
        meta=meta,
    )


def _candidate() -> ContentCandidate:
    return ContentCandidate(
        id=55,
        source_document_id=44,
        channel_id=7,
        status="new",
        suggested_action="rewrite",
        meta={"reuse_policy": "rewrite_with_attribution"},
    )


def test_origin_requires_explicit_persisted_transport_marker() -> None:
    ordinary = {
        "telegram_sender": {"user": {"username": "alice"}},
        "telegram_suggested_post_info": {"state": "pending", "price": {"amount": 10}},
    }
    wrong_transport = {**ordinary, "transport": "telegram"}

    assert project_suggested_post_inbox(ordinary) is None
    assert project_suggested_post_inbox(wrong_transport) is None


def test_pending_free_projection_exposes_safe_sender_topic_and_native_identity() -> None:
    view = project_suggested_post_inbox(_base_meta())

    assert view is not None
    assert view.native_status == "pending"
    assert view.commercial_kind == "free"
    assert view.sender is not None and view.sender.display_name == "@alice"
    assert view.topic_user is not None and view.topic_user.id == 501
    assert view.topic_id == 123
    assert view.direct_messages_chat_id == -1009001
    assert view.message_id == 77
    assert view.proposed_send_date == datetime.fromtimestamp(1787184000, tz=timezone.utc)
    assert view.price is None
    assert view.payment is None


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("approved", "approved"),
        ("declined", "declined"),
        ("approval_failed", "approval_failed"),
        ("paid", "paid"),
        ("refunded", "refunded"),
    ],
)
def test_lifecycle_event_is_presented_without_synthesizing_intermediate_states(
    event: str,
    expected: str,
) -> None:
    meta = _base_meta()
    meta["telegram_suggested_post_lifecycle"] = {
        "event": event,
        "service_message_id": 900,
        "payload": {},
    }

    view = project_suggested_post_inbox(meta)

    assert view is not None
    assert view.native_status == expected


def test_paid_and_refunded_business_state_stays_separate_from_candidate_state() -> None:
    meta = _base_meta()
    meta["telegram_suggested_post_info"]["price"] = {"currency": "XTR", "amount": 120}
    meta["telegram_suggested_post_paid"] = {
        "event": "paid",
        "payload": {
            "currency": "XTR",
            "star_amount": {"amount": 120, "nanostar_amount": 5},
        },
    }
    meta["telegram_suggested_post_lifecycle"] = {
        "event": "refunded",
        "payload": {"reason": "post_deleted"},
    }
    meta["telegram_suggested_post_refunded"] = {
        "event": "refunded",
        "payload": {"reason": "post_deleted"},
    }

    response = _candidate_response(_candidate(), _document(meta))

    assert response.id == 55
    assert response.source_document_id == 44
    assert response.status == "new"
    assert response.suggested_post is not None
    assert response.suggested_post.native_status == "refunded"
    assert response.suggested_post.commercial_kind == "paid"
    assert response.suggested_post.price is not None
    assert response.suggested_post.price.amount == 120
    assert response.suggested_post.payment is not None
    assert response.suggested_post.payment.amount == 120
    assert response.suggested_post.payment.nanostar_amount == 5
    assert response.suggested_post.refund_reason == "post_deleted"


def test_lifecycle_refresh_changes_read_model_not_candidate_card_identity() -> None:
    candidate = _candidate()
    document = _document(_base_meta())
    pending = _candidate_response(candidate, document)

    document.meta = {
        **dict(document.meta or {}),
        "telegram_suggested_post_lifecycle": {
            "event": "approved",
            "payload": {"send_date": "2026-08-20T12:30:00Z"},
        },
        "telegram_suggested_post_approved": {
            "event": "approved",
            "payload": {"send_date": "2026-08-20T12:30:00Z"},
        },
    }
    approved = _candidate_response(candidate, document)

    assert pending.id == approved.id == 55
    assert pending.source_document_id == approved.source_document_id == 44
    assert pending.suggested_post is not None
    assert approved.suggested_post is not None
    assert pending.suggested_post.native_status == "pending"
    assert approved.suggested_post.native_status == "approved"
    assert approved.suggested_post.proposed_send_date == datetime(
        2026, 8, 20, 12, 30, tzinfo=timezone.utc
    )


def test_missing_optional_business_and_sender_fields_degrade_without_guessing() -> None:
    view = project_suggested_post_inbox(
        {
            "transport": "telegram_suggested_posts",
            "telegram_direct_messages_chat_id": -1009001,
            "telegram_message_id": 77,
            "telegram_suggested_post_info": {"state": "approved"},
        }
    )

    assert view is not None
    assert view.native_status == "approved"
    assert view.commercial_kind == "free"
    assert view.sender is None
    assert view.topic_user is None
    assert view.topic_id is None
    assert view.proposed_send_date is None
    assert view.price is None
    assert view.payment is None


def test_malformed_or_unknown_newer_lifecycle_is_neutral_instead_of_reviving_pending() -> None:
    malformed = _base_meta()
    malformed["telegram_suggested_post_lifecycle"] = {"event": "future_state", "payload": []}

    view = project_suggested_post_inbox(malformed)

    assert view is not None
    assert view.native_status == "unknown"
    assert view.commercial_kind == "free"


def test_decline_and_refund_details_are_read_only_provenance() -> None:
    declined = _base_meta()
    declined["telegram_suggested_post_lifecycle"] = {
        "event": "declined",
        "payload": {"comment": "Not a fit"},
    }
    declined["telegram_suggested_post_declined"] = {
        "event": "declined",
        "payload": {"comment": "Not a fit"},
    }
    declined_view = project_suggested_post_inbox(declined)

    refunded = _base_meta()
    refunded["telegram_suggested_post_lifecycle"] = {
        "event": "refunded",
        "payload": {"reason": "payment_refunded"},
    }
    refunded_view = project_suggested_post_inbox(refunded)

    assert declined_view is not None and declined_view.decline_comment == "Not a fit"
    assert refunded_view is not None and refunded_view.refund_reason == "payment_refunded"
