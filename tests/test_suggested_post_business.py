from datetime import datetime, timezone

from app.services.suggested_post_business import (
    classify_refund_reason,
    effective_native_state,
    paid_proposal_is_safe_to_approve,
    parse_suggested_post_payment,
    parse_suggested_post_price,
)


def test_price_units_are_tagged_not_collapsed() -> None:
    stars = parse_suggested_post_price({"currency": "XTR", "amount": 25})
    ton = parse_suggested_post_price({"currency": "TON", "amount": 50_000_000})
    unknown = parse_suggested_post_price({"currency": "ABC", "amount": 7})

    assert stars is not None
    assert stars.kind == "stars" and stars.stars == 25 and stars.ton_nanograms is None
    assert ton is not None
    assert ton.kind == "ton_nanograms" and ton.ton_nanograms == 50_000_000
    assert ton.stars is None
    assert unknown is not None
    assert unknown.kind == "unknown" and unknown.raw_amount == 7


def test_payment_wire_shapes_remain_currency_specific() -> None:
    stars = parse_suggested_post_payment(
        {
            "currency": "XTR",
            "star_amount": {"amount": 10, "nanostar_amount": 3},
        }
    )
    ton = parse_suggested_post_payment({"currency": "TON", "amount": 40_000_000})

    assert stars is not None
    assert stars.kind == "stars" and stars.stars == 10 and stars.nanostar_amount == 3
    assert stars.ton_nanograms is None
    assert ton is not None
    assert ton.kind == "ton_nanograms" and ton.ton_nanograms == 40_000_000
    assert ton.stars is None


def test_refund_reasons_are_explicit_and_unknown_stays_unknown() -> None:
    assert classify_refund_reason("post_deleted") == "post_deleted"
    assert classify_refund_reason("payment_refunded") == "payment_refunded"
    assert classify_refund_reason("future_reason") == "unknown"


def test_newer_provider_service_message_wins_over_arrival_order() -> None:
    metadata = {
        "telegram_suggested_post_refunded": {
            "event": "refunded",
            "service_message_id": 920,
            "service_message_date": datetime(
                2026, 8, 18, 4, tzinfo=timezone.utc
            ).isoformat(),
            "payload": {"reason": "post_deleted"},
        },
        "telegram_suggested_post_paid": {
            "event": "paid",
            "service_message_id": 910,
            "service_message_date": datetime(
                2026, 8, 18, 2, tzinfo=timezone.utc
            ).isoformat(),
            "payload": {"currency": "XTR", "star_amount": {"amount": 10}},
        },
        # Older paid update happened to arrive last.
        "telegram_suggested_post_lifecycle": {
            "event": "paid",
            "service_message_id": 910,
            "payload": {},
        },
    }
    assert effective_native_state(metadata) == "refunded"


def test_paid_approve_terms_fail_closed_only_when_persisted_price_is_unknown() -> None:
    assert paid_proposal_is_safe_to_approve(
        {"telegram_suggested_post_info": {"state": "pending"}}
    )
    assert paid_proposal_is_safe_to_approve(
        {
            "telegram_suggested_post_info": {
                "state": "pending",
                "price": {"currency": "XTR", "amount": 10},
            }
        }
    )
    assert paid_proposal_is_safe_to_approve(
        {
            "telegram_suggested_post_info": {
                "state": "pending",
                "price": {"currency": "TON", "amount": 50_000_000},
            }
        }
    )
    assert not paid_proposal_is_safe_to_approve(
        {
            "telegram_suggested_post_info": {
                "state": "pending",
                "price": {"currency": "FUTURE", "amount": 7},
            }
        }
    )
