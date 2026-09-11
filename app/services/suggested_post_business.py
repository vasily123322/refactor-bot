from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


SuggestedPostMoneyKind = Literal["stars", "ton_nanograms", "unknown"]
SuggestedPostRefundReason = Literal["post_deleted", "payment_refunded", "unknown"]

KNOWN_LIFECYCLE_EVENTS = frozenset(
    {"approved", "declined", "approval_failed", "paid", "refunded"}
)
INITIAL_NATIVE_STATES = frozenset({"pending", "approved", "declined"})


@dataclass(frozen=True, slots=True)
class SuggestedPostMoney:
    kind: SuggestedPostMoneyKind
    currency: str | None
    stars: int | None = None
    nanostar_amount: int | None = None
    ton_nanograms: int | None = None
    raw_amount: int | None = None

    @property
    def understood(self) -> bool:
        return self.kind != "unknown"


@dataclass(frozen=True, slots=True)
class SuggestedPostLifecycleFact:
    event: str
    service_message_id: int | None
    event_at: datetime | None
    payload: Mapping[str, Any]
    origin: str | None = None


def mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def integer(value: Any) -> int | None:
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


def text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def utc_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    numeric = integer(value)
    if numeric is not None:
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw = text(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _valid_nonnegative(value: Any) -> int | None:
    parsed = integer(value)
    return parsed if parsed is not None and parsed >= 0 else None


def parse_suggested_post_price(value: Any) -> SuggestedPostMoney | None:
    """Parse SuggestedPostPrice without collapsing Stars and TON units."""

    raw = mapping(value)
    if raw is None:
        return None
    currency = text(raw.get("currency"))
    amount = _valid_nonnegative(raw.get("amount"))
    if currency == "XTR" and amount is not None:
        return SuggestedPostMoney(kind="stars", currency="XTR", stars=amount)
    if currency == "TON" and amount is not None:
        return SuggestedPostMoney(
            kind="ton_nanograms",
            currency="TON",
            ton_nanograms=amount,
        )
    if currency is None and amount is None:
        return None
    return SuggestedPostMoney(
        kind="unknown",
        currency=currency,
        raw_amount=amount,
    )


def parse_suggested_post_payment(value: Any) -> SuggestedPostMoney | None:
    """Parse SuggestedPostPaid while preserving its currency-specific wire shape."""

    raw = mapping(value)
    if raw is None:
        return None
    currency = text(raw.get("currency"))
    if currency == "XTR":
        star = mapping(raw.get("star_amount"))
        if star is None:
            return SuggestedPostMoney(kind="unknown", currency="XTR")
        stars = _valid_nonnegative(star.get("amount"))
        nanostar = _valid_nonnegative(star.get("nanostar_amount"))
        if stars is None:
            return SuggestedPostMoney(kind="unknown", currency="XTR")
        return SuggestedPostMoney(
            kind="stars",
            currency="XTR",
            stars=stars,
            nanostar_amount=nanostar,
        )
    if currency == "TON":
        amount = _valid_nonnegative(raw.get("amount"))
        if amount is None:
            return SuggestedPostMoney(kind="unknown", currency="TON")
        return SuggestedPostMoney(
            kind="ton_nanograms",
            currency="TON",
            ton_nanograms=amount,
        )
    raw_amount = _valid_nonnegative(raw.get("amount"))
    if currency is None and raw_amount is None and raw.get("star_amount") is None:
        return None
    return SuggestedPostMoney(
        kind="unknown",
        currency=currency,
        raw_amount=raw_amount,
    )


def classify_refund_reason(value: Any) -> SuggestedPostRefundReason:
    reason = text(value)
    if reason in {"post_deleted", "payment_refunded"}:
        return reason  # type: ignore[return-value]
    return "unknown"


def lifecycle_fact(value: Any) -> SuggestedPostLifecycleFact | None:
    raw = mapping(value)
    if raw is None:
        return None
    event = text(raw.get("event"))
    if event not in KNOWN_LIFECYCLE_EVENTS:
        return None
    payload = mapping(raw.get("payload")) or {}
    service_message_id = integer(raw.get("service_message_id"))
    if service_message_id is not None and service_message_id <= 0:
        service_message_id = None
    return SuggestedPostLifecycleFact(
        event=event,
        service_message_id=service_message_id,
        event_at=utc_timestamp(raw.get("service_message_date")),
        payload=payload,
        origin=text(raw.get("origin")),
    )


def event_fact(metadata: Mapping[str, Any], event: str) -> SuggestedPostLifecycleFact | None:
    return lifecycle_fact(metadata.get(f"telegram_suggested_post_{event}"))


def effective_lifecycle_fact(metadata: Mapping[str, Any]) -> SuggestedPostLifecycleFact | None:
    """Select the newest provider-observed lifecycle event without trusting arrival order.

    Telegram service message ids are scoped to the already-proven same DM chat and form
    the strongest ordering fact we persist. Synthetic Studio action results intentionally
    have no service message id and remain a fallback until Telegram lifecycle evidence
    arrives. This prevents a delayed older provider update from reviving an older state.
    """

    provider_facts = [
        fact
        for event in KNOWN_LIFECYCLE_EVENTS
        if (fact := event_fact(metadata, event)) is not None
        and fact.service_message_id is not None
    ]
    if provider_facts:
        return max(
            provider_facts,
            key=lambda fact: (
                int(fact.service_message_id or 0),
                fact.event_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
    return lifecycle_fact(metadata.get("telegram_suggested_post_lifecycle"))


def effective_native_state(metadata: Mapping[str, Any]) -> str:
    fact = effective_lifecycle_fact(metadata)
    if fact is not None:
        return fact.event
    if "telegram_suggested_post_lifecycle" in metadata:
        return "unknown"
    info = mapping(metadata.get("telegram_suggested_post_info"))
    state = text(info.get("state")) if info is not None else None
    return state if state in INITIAL_NATIVE_STATES else "unknown"


def proposed_price(metadata: Mapping[str, Any]) -> SuggestedPostMoney | None:
    """Return the latest understood proposal/approval price, never a payment receipt."""

    current = effective_lifecycle_fact(metadata)
    approved = event_fact(metadata, "approved")
    approval_failed = event_fact(metadata, "approval_failed")
    info = mapping(metadata.get("telegram_suggested_post_info"))
    values = [
        current.payload.get("price") if current is not None else None,
        approved.payload.get("price") if approved is not None else None,
        info.get("price") if info is not None else None,
        approval_failed.payload.get("price") if approval_failed is not None else None,
    ]
    for value in values:
        parsed = parse_suggested_post_price(value)
        if parsed is not None:
            return parsed
    return None


def paid_payment(metadata: Mapping[str, Any]) -> SuggestedPostMoney | None:
    paid = event_fact(metadata, "paid")
    if paid is not None:
        return parse_suggested_post_payment(paid.payload)
    current = effective_lifecycle_fact(metadata)
    if current is not None and current.event == "paid":
        return parse_suggested_post_payment(current.payload)
    return None


def paid_proposal_is_safe_to_approve(metadata: Mapping[str, Any]) -> bool:
    """Fail closed only when a persisted price exists but its currency/value is unknown."""

    info = mapping(metadata.get("telegram_suggested_post_info"))
    if info is None or "price" not in info:
        return True
    parsed = parse_suggested_post_price(info.get("price"))
    return parsed is not None and parsed.understood
