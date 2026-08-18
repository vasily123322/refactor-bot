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
            "user": {"id": 501, "username": "alice", "first_name": "Alice"},
        },
        "telegram_sender": {
            "user": {"id": 501, "username": "alice", "first_name": "Alice"}
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
        meta={"reuse_policy": "rewrite_with_attribution", "rewrite_run_id": 42},
    )


def _event(event: str, message_id: int, payload: dict, hour: int) -> dict:
    return {
        "event": event,
        "service_message_id": message_id,
        "service_message_date": datetime(2026, 8, 18, hour, tzinfo=timezone.utc).isoformat(),
        "payload": payload,
    }


def test_origin_requires_explicit_persisted_transport_marker() -> None:
    ordinary = {
        "telegram_sender": {"user": {"username": "alice"}},
        "telegram_suggested_post_info": {"state": "pending", "price": {"amount": 10}},
    }
    assert project_suggested_post_inbox(ordinary) is None
    assert project_suggested_post_inbox({**ordinary, "transport": "telegram"}) is None


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
    event: str, expected: str
) -> None:
    meta = _base_meta()
    meta["telegram_suggested_post_lifecycle"] = _event(event, 900, {}, 1)
    view = project_suggested_post_inbox(meta)
    assert view is not None
    assert view.native_status == expected


def test_xtr_payment_is_tagged_and_stays_separate_from_candidate_state() -> None:
    meta = _base_meta()
    meta["telegram_suggested_post_info"]["price"] = {"currency": "XTR", "amount": 120}
    meta["telegram_suggested_post_paid"] = _event(
        "paid",
        910,
        {"currency": "XTR", "star_amount": {"amount": 120, "nanostar_amount": 5}},
        2,
    )
    meta["telegram_suggested_post_lifecycle"] = meta["telegram_suggested_post_paid"]

    response = _candidate_response(_candidate(), _document(meta))
    view = response.suggested_post
    assert response.status == "new"
    assert _candidate().meta["rewrite_run_id"] == 42
    assert view is not None and view.native_status == "paid"
    assert view.price is not None
    assert view.price.kind == "stars" and view.price.stars == 120
    assert view.price.ton_nanograms is None
    assert view.payment is not None
    assert view.payment.kind == "stars" and view.payment.stars == 120
    assert view.payment.nanostar_amount == 5
    assert view.paid_service_message_id == 910
    assert view.paid_event_at == datetime(2026, 8, 18, 2, tzinfo=timezone.utc)


def test_ton_payment_never_collapses_nanograms_into_stars_or_generic_amount() -> None:
    meta = _base_meta()
    meta["telegram_suggested_post_info"]["price"] = {
        "currency": "TON",
        "amount": 50_000_000,
    }
    meta["telegram_suggested_post_paid"] = _event(
        "paid", 911, {"currency": "TON", "amount": 49_000_000}, 3
    )
    meta["telegram_suggested_post_lifecycle"] = meta["telegram_suggested_post_paid"]
    view = project_suggested_post_inbox(meta)
    assert view is not None and view.price is not None and view.payment is not None
    assert view.price.kind == "ton_nanograms"
    assert view.price.ton_nanograms == 50_000_000
    assert view.price.stars is None
    assert view.payment.kind == "ton_nanograms"
    assert view.payment.ton_nanograms == 49_000_000
    assert not hasattr(view.payment, "amount")


def test_unknown_currency_is_preserved_raw_but_not_interpreted() -> None:
    meta = _base_meta()
    meta["telegram_suggested_post_info"]["price"] = {"currency": "ABC", "amount": 77}
    view = project_suggested_post_inbox(meta)
    assert view is not None and view.price is not None
    assert view.price.kind == "unknown"
    assert view.price.currency == "ABC"
    assert view.price.raw_amount == 77
    assert view.commercial_kind == "unknown"


@pytest.mark.parametrize("reason", ["post_deleted", "payment_refunded"])
def test_refund_reason_codes_remain_distinct_business_provenance(reason: str) -> None:
    meta = _base_meta()
    meta["telegram_suggested_post_paid"] = _event(
        "paid", 910, {"currency": "XTR", "star_amount": {"amount": 10}}, 2
    )
    meta["telegram_suggested_post_refunded"] = _event(
        "refunded", 920, {"reason": reason}, 4
    )
    meta["telegram_suggested_post_lifecycle"] = meta["telegram_suggested_post_refunded"]
    response = _candidate_response(_candidate(), _document(meta))
    view = response.suggested_post
    assert view is not None and view.native_status == "refunded"
    assert view.refund_reason == reason
    assert view.refund_reason_code == reason
    assert view.refunded_service_message_id == 920
    assert response.status == "new"
    assert response.source_document_id == 44


def test_out_of_order_older_paid_event_cannot_regress_newer_refund() -> None:
    meta = _base_meta()
    refund = _event("refunded", 920, {"reason": "post_deleted"}, 4)
    older_paid = _event(
        "paid", 910, {"currency": "XTR", "star_amount": {"amount": 10}}, 2
    )
    meta["telegram_suggested_post_refunded"] = refund
    meta["telegram_suggested_post_paid"] = older_paid
    # Simulate the older update arriving last and overwriting only the compatibility current key.
    meta["telegram_suggested_post_lifecycle"] = older_paid
    view = project_suggested_post_inbox(meta)
    assert view is not None
    assert view.native_status == "refunded"
    assert view.refund_reason_code == "post_deleted"


def test_lifecycle_refresh_changes_read_model_not_candidate_card_identity() -> None:
    candidate = _candidate()
    document = _document(_base_meta())
    pending = _candidate_response(candidate, document)
    approved_event = _event(
        "approved", 905, {"send_date": "2026-08-20T12:30:00Z"}, 1
    )
    document.meta = {
        **dict(document.meta or {}),
        "telegram_suggested_post_lifecycle": approved_event,
        "telegram_suggested_post_approved": approved_event,
    }
    approved = _candidate_response(candidate, document)
    assert pending.id == approved.id == 55
    assert pending.source_document_id == approved.source_document_id == 44
    assert pending.suggested_post is not None and approved.suggested_post is not None
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
    assert view.sender is None and view.topic_user is None
    assert view.price is None and view.payment is None


def test_malformed_or_unknown_newer_lifecycle_is_neutral_instead_of_reviving_pending() -> None:
    malformed = _base_meta()
    malformed["telegram_suggested_post_lifecycle"] = {"event": "future_state", "payload": []}
    view = project_suggested_post_inbox(malformed)
    assert view is not None
    assert view.native_status == "unknown"
    assert view.commercial_kind == "unknown"


def test_decline_comment_is_read_only_provenance() -> None:
    meta = _base_meta()
    declined = _event("declined", 906, {"comment": "Not a fit"}, 1)
    meta["telegram_suggested_post_lifecycle"] = declined
    meta["telegram_suggested_post_declined"] = declined
    view = project_suggested_post_inbox(meta)
    assert view is not None and view.decline_comment == "Not a fit"
