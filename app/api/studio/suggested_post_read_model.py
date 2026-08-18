from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel

from app.services.suggested_post_business import (
    SuggestedPostMoney,
    classify_refund_reason,
    effective_lifecycle_fact,
    effective_native_state,
    event_fact,
    integer,
    mapping,
    paid_payment,
    proposed_price,
    text,
    utc_timestamp,
)


SuggestedPostNativeStatus = Literal[
    "pending",
    "approved",
    "declined",
    "approval_failed",
    "paid",
    "refunded",
    "unknown",
]
SuggestedPostCommercialKind = Literal["free", "paid", "unknown"]
SuggestedPostMoneyKind = Literal["stars", "ton_nanograms", "unknown"]
SuggestedPostRefundReasonCode = Literal["post_deleted", "payment_refunded", "unknown"]


class SuggestedPostPersonView(BaseModel):
    id: int | None = None
    username: str | None = None
    display_name: str | None = None


class SuggestedPostMoneyView(BaseModel):
    kind: SuggestedPostMoneyKind
    currency: str | None = None
    stars: int | None = None
    nanostar_amount: int | None = None
    ton_nanograms: int | None = None
    raw_amount: int | None = None


class SuggestedPostInboxView(BaseModel):
    transport: Literal["telegram_suggested_posts"] = "telegram_suggested_posts"
    native_status: SuggestedPostNativeStatus
    commercial_kind: SuggestedPostCommercialKind
    sender: SuggestedPostPersonView | None = None
    topic_user: SuggestedPostPersonView | None = None
    topic_id: int | None = None
    direct_messages_chat_id: int | None = None
    message_id: int | None = None
    proposed_send_date: datetime | None = None
    price: SuggestedPostMoneyView | None = None
    payment: SuggestedPostMoneyView | None = None
    paid_event_at: datetime | None = None
    paid_service_message_id: int | None = None
    refunded_event_at: datetime | None = None
    refunded_service_message_id: int | None = None
    decline_comment: str | None = None
    refund_reason: str | None = None
    refund_reason_code: SuggestedPostRefundReasonCode | None = None


def _person(value: Any) -> SuggestedPostPersonView | None:
    raw = mapping(value)
    if raw is None:
        return None
    user_id = integer(raw.get("id"))
    username = text(raw.get("username"))
    first_name = text(raw.get("first_name"))
    last_name = text(raw.get("last_name"))
    if username:
        display_name = f"@{username.lstrip('@')}"
    else:
        display_name = " ".join(part for part in (first_name, last_name) if part) or None
    if user_id is None and username is None and display_name is None:
        return None
    return SuggestedPostPersonView(
        id=user_id,
        username=username,
        display_name=display_name,
    )


def _money(value: SuggestedPostMoney | None) -> SuggestedPostMoneyView | None:
    if value is None:
        return None
    return SuggestedPostMoneyView(
        kind=value.kind,
        currency=value.currency,
        stars=value.stars,
        nanostar_amount=value.nanostar_amount,
        ton_nanograms=value.ton_nanograms,
        raw_amount=value.raw_amount,
    )


def _first_timestamp(*values: Any) -> datetime | None:
    for value in values:
        parsed = utc_timestamp(value)
        if parsed is not None:
            return parsed
    return None


def project_suggested_post_inbox(
    metadata: Mapping[str, Any] | None,
) -> SuggestedPostInboxView | None:
    """Project persisted Suggested Post provenance without creating product authority.

    Payment values stay tagged by Telegram currency/unit, lifecycle ordering uses the
    provider service-message evidence retained by T4.2, and all native business facts
    remain independent from candidate/Content/publication lifecycle.
    """

    raw = mapping(metadata)
    if raw is None or raw.get("transport") != "telegram_suggested_posts":
        return None

    native = effective_native_state(raw)
    if native in {
        "pending",
        "approved",
        "declined",
        "approval_failed",
        "paid",
        "refunded",
    }:
        native_status = cast(SuggestedPostNativeStatus, native)
    else:
        native_status = "unknown"

    info = mapping(raw.get("telegram_suggested_post_info"))
    initial_state = text(info.get("state")) if info is not None else None
    current = effective_lifecycle_fact(raw)
    approved = event_fact(raw, "approved")
    declined = event_fact(raw, "declined")
    paid = event_fact(raw, "paid")
    refunded = event_fact(raw, "refunded")

    price_value = proposed_price(raw)
    payment_value = paid_payment(raw)
    price = _money(price_value)
    payment = _money(payment_value)

    if native_status in {"paid", "refunded"}:
        commercial_kind: SuggestedPostCommercialKind = "paid"
    elif (price_value is not None and price_value.understood) or (
        payment_value is not None and payment_value.understood
    ):
        commercial_kind = "paid"
    elif price_value is not None or payment_value is not None:
        commercial_kind = "unknown"
    elif "telegram_suggested_post_lifecycle" in raw and native_status == "unknown":
        commercial_kind = "unknown"
    elif native_status == "approval_failed":
        commercial_kind = "unknown"
    elif native_status == "approved" and current is not None:
        commercial_kind = "free"
    elif initial_state in {"pending", "approved", "declined"}:
        commercial_kind = "free"
    else:
        commercial_kind = "unknown"

    topic = mapping(raw.get("telegram_direct_messages_topic"))
    sender = mapping(raw.get("telegram_sender"))
    sender_user = mapping(sender.get("user")) if sender is not None else None

    proposed_send_date = _first_timestamp(
        approved.payload.get("send_date") if approved is not None else None,
        current.payload.get("send_date") if current is not None else None,
        info.get("send_date") if info is not None else None,
    )

    decline_fact = declined if declined is not None else (
        current if current is not None and current.event == "declined" else None
    )
    refund_fact = refunded if refunded is not None else (
        current if current is not None and current.event == "refunded" else None
    )
    refund_reason = (
        text(refund_fact.payload.get("reason")) if refund_fact is not None else None
    )

    return SuggestedPostInboxView(
        native_status=native_status,
        commercial_kind=commercial_kind,
        sender=_person(sender_user),
        topic_user=_person(topic.get("user") if topic is not None else None),
        topic_id=integer(topic.get("topic_id")) if topic is not None else None,
        direct_messages_chat_id=integer(raw.get("telegram_direct_messages_chat_id")),
        message_id=integer(raw.get("telegram_message_id")),
        proposed_send_date=proposed_send_date,
        price=price,
        payment=payment,
        paid_event_at=paid.event_at if paid is not None else None,
        paid_service_message_id=paid.service_message_id if paid is not None else None,
        refunded_event_at=refunded.event_at if refunded is not None else None,
        refunded_service_message_id=(
            refunded.service_message_id if refunded is not None else None
        ),
        decline_comment=(
            text(decline_fact.payload.get("comment"))
            if decline_fact is not None
            else None
        ),
        refund_reason=refund_reason,
        refund_reason_code=(
            classify_refund_reason(refund_reason) if refund_reason is not None else None
        ),
    )
