from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
)


class CanonicalPublicationDeliveryPostSendBlockingError(RuntimeError):
    """Required post-send semantics could not be durably established.

    The primary provider may already have produced Telegram side effects. Callers must
    therefore leave the delivery claim ambiguous for recovery rather than finalize
    success or retry primary delivery automatically.
    """


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryPostSendContext:
    """Immutable evidence available after primary send and before terminal commit."""

    publication_id: int
    lease: CanonicalPublicationDeliveryLeaseHandle
    plan: CanonicalPublicationDeliveryPlan
    message_ids: tuple[int, ...]
    result_link: str | None
    primary_finished_at: datetime


class CanonicalPublicationDeliveryPostSendHook(Protocol):
    """One-shot auxiliary work executed while the exact delivery lease remains live."""

    async def execute(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> None: ...
