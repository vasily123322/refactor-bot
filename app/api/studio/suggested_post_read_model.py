from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel


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

_KNOWN_NATIVE_STATUSES = {
    "pending",
    "approved",
    "declined",
    "approval_failed",
    "paid",
    "refunded",
}
_INITIAL_NATIVE_STATUSES = {"pending", "approved", "declined"}


class SuggestedPostPersonView(BaseModel):
    id: int | None = None
    username: str | None = None
    display_name: str | None = None


class SuggestedPostMoneyView(BaseModel):
    currency: str | None = None
    amount: int | None = None
    nanostar_amount: int | None = None


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
    decline_comment: str | None = None
    refund_reason: str | None = None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text and text.lstrip("-").isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    numeric = _integer(value)
    if numeric is not None:
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _person(value: Any) -> SuggestedPostPersonView | None:
    raw = _mapping(value)
    if raw is None:
        return None
    user_id = _integer(raw.get("id"))
    username = _text(raw.get("username"))
    first_name = _text(raw.get("first_name"))
    last_name = _text(raw.get("last_name"))
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


def _money(value: Any) -> SuggestedPostMoneyView | None:
    raw = _mapping(value)
    if raw is None:
        return None

    currency = _text(raw.get("currency"))
    amount = _integer(raw.get("amount"))
    nanostar_amount = _integer(raw.get("nanostar_amount"))

    star_amount = _mapping(raw.get("star_amount"))
    if star_amount is not None:
        amount = _integer(star_amount.get("amount"))
        nanostar_amount = _integer(star_amount.get("nanostar_amount"))
        currency = currency or "XTR"

    if currency is None and amount is None and nanostar_amount is None:
        return None
    return SuggestedPostMoneyView(
        currency=currency,
        amount=amount,
        nanostar_amount=nanostar_amount,
    )


def _event_payload(metadata: Mapping[str, Any], event: str) -> Mapping[str, Any] | None:
    event_meta = _mapping(metadata.get(f"telegram_suggested_post_{event}"))
    if event_meta is None:
        return None
    return _mapping(event_meta.get("payload"))


def _current_lifecycle_payload(
    lifecycle: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if lifecycle is None:
        return None
    return _mapping(lifecycle.get("payload"))


def _first_money(*values: Any) -> SuggestedPostMoneyView | None:
    for value in values:
        parsed = _money(value)
        if parsed is not None:
            return parsed
    return None


def _first_timestamp(*values: Any) -> datetime | None:
    for value in values:
        parsed = _timestamp(value)
        if parsed is not None:
            return parsed
    return None


def _native_status(
    metadata: Mapping[str, Any],
    info: Mapping[str, Any] | None,
) -> SuggestedPostNativeStatus:
    if "telegram_suggested_post_lifecycle" in metadata:
        lifecycle = _mapping(metadata.get("telegram_suggested_post_lifecycle"))
        event = _text(lifecycle.get("event")) if lifecycle is not None else None
        if event in _KNOWN_NATIVE_STATUSES:
            return event  # type: ignore[return-value]
        return "unknown"

    state = _text(info.get("state")) if info is not None else None
    if state in _INITIAL_NATIVE_STATUSES:
        return state  # type: ignore[return-value]
    return "unknown"


def project_suggested_post_inbox(
    metadata: Mapping[str, Any] | None,
) -> SuggestedPostInboxView | None:
    """Project persisted T4.2 provenance into a narrow, read-only Inbox model.

    The explicit transport marker is the only origin discriminator. Unknown or
    malformed lifecycle data degrades to a neutral state instead of reviving an
    older Telegram state. No routing, Content, approval, or publication authority
    is derived here.
    """

    raw = _mapping(metadata)
    if raw is None or raw.get("transport") != "telegram_suggested_posts":
        return None

    info = _mapping(raw.get("telegram_suggested_post_info"))
    lifecycle = _mapping(raw.get("telegram_suggested_post_lifecycle"))
    lifecycle_payload = _current_lifecycle_payload(lifecycle)
    native_status = _native_status(raw, info)

    approved_payload = _event_payload(raw, "approved")
    approval_failed_payload = _event_payload(raw, "approval_failed")
    paid_payload = _event_payload(raw, "paid")
    declined_payload = _event_payload(raw, "declined")
    refunded_payload = _event_payload(raw, "refunded")

    price = _first_money(
        info.get("price") if info is not None else None,
        lifecycle_payload.get("price") if lifecycle_payload is not None else None,
        approved_payload.get("price") if approved_payload is not None else None,
        approval_failed_payload.get("price")
        if approval_failed_payload is not None
        else None,
    )
    payment = _first_money(
        paid_payload,
        lifecycle_payload if native_status == "paid" else None,
    )
    if price is not None or payment is not None:
        commercial_kind: SuggestedPostCommercialKind = "paid"
    elif info is not None:
        # Bot API SuggestedPostInfo.price is optional; its absence means an unpaid
        # proposal. Only explicit persisted SuggestedPostInfo can establish this.
        commercial_kind = "free"
    else:
        commercial_kind = "unknown"

    topic = _mapping(raw.get("telegram_direct_messages_topic"))
    sender = _mapping(raw.get("telegram_sender"))
    sender_user = _mapping(sender.get("user")) if sender is not None else None

    proposed_send_date = _first_timestamp(
        info.get("send_date") if info is not None else None,
        approved_payload.get("send_date") if approved_payload is not None else None,
        lifecycle_payload.get("send_date") if lifecycle_payload is not None else None,
    )

    decline_payload = declined_payload
    if decline_payload is None and native_status == "declined":
        decline_payload = lifecycle_payload
    refund_payload = refunded_payload
    if refund_payload is None and native_status == "refunded":
        refund_payload = lifecycle_payload

    return SuggestedPostInboxView(
        native_status=native_status,
        commercial_kind=commercial_kind,
        sender=_person(sender_user),
        topic_user=_person(topic.get("user") if topic is not None else None),
        topic_id=_integer(topic.get("topic_id")) if topic is not None else None,
        direct_messages_chat_id=_integer(raw.get("telegram_direct_messages_chat_id")),
        message_id=_integer(raw.get("telegram_message_id")),
        proposed_send_date=proposed_send_date,
        price=price,
        payment=payment,
        decline_comment=(
            _text(decline_payload.get("comment"))
            if decline_payload is not None
            else None
        ),
        refund_reason=(
            _text(refund_payload.get("reason")) if refund_payload is not None else None
        ),
    )
