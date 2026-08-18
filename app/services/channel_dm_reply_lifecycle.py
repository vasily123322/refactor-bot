from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.services.telegram_channel_dms import TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND


_MAX_HISTORY = 5
_MAX_BATCH = 100
_ALLOWED_STATES = frozenset({"pending", "sent", "failed", "uncertain"})
_SAFE_ERROR_CLASSES = frozenset(
    {
        "routing_mismatch",
        "routing_check_unavailable",
        "rights_check_unavailable",
        "insufficient_rights",
        "provider_forbidden",
        "native_reply_target_missing",
        "provider_rejected",
        "provider_retry_after",
        "provider_outcome_unknown",
        "provider_confirmation_malformed",
    }
)


class ChannelDMReplyLifecycleFailure(str, Enum):
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    ROUTING_MISMATCH = "routing_mismatch"
    MALFORMED_LIFECYCLE = "malformed_lifecycle"
    INVALID_REQUEST = "invalid_request"


class ChannelDMReplyLifecycleError(RuntimeError):
    def __init__(self, failure: ChannelDMReplyLifecycleFailure, message: str) -> None:
        self.failure = failure
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ChannelDMReplyLifecycleItem:
    command_id: int
    reply_text: str
    state: str
    requested_at: datetime
    sent_at: datetime | None
    finished_at: datetime | None
    error_class: str | None


@dataclass(frozen=True, slots=True)
class ChannelDMReplyLifecycleResult:
    candidate_id: int
    commands: tuple[ChannelDMReplyLifecycleItem, ...]


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _safe_error_class(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized if normalized in _SAFE_ERROR_CLASSES else "delivery_error"


def _lifecycle_item(command: ChannelDMReplyCommand) -> ChannelDMReplyLifecycleItem:
    state = str(command.state)
    if state not in _ALLOWED_STATES or command.created_at is None:
        raise ChannelDMReplyLifecycleError(
            ChannelDMReplyLifecycleFailure.MALFORMED_LIFECYCLE,
            "stored Channel-DM reply lifecycle is malformed",
        )
    finished_at = command.finished_at
    return ChannelDMReplyLifecycleItem(
        command_id=int(command.id),
        reply_text=str(command.reply_text),
        state=state,
        requested_at=command.created_at,
        sent_at=finished_at if state == "sent" else None,
        finished_at=finished_at,
        error_class=_safe_error_class(command.error_class),
    )


class ChannelDMReplyLifecycleReader:
    """Read-only projection of the durable T5.3 reply command ledger.

    Historical delivery evidence is deliberately independent from current Telegram
    rights and mutable connector enablement. Reading it never revalidates delivery,
    reconciles ambiguous provider outcomes, or authorizes another send.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def read(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
        limit: int = _MAX_HISTORY,
    ) -> ChannelDMReplyLifecycleResult:
        results = await self.read_many(
            candidate_ids=[candidate_id],
            actor_client_id=actor_client_id,
            limit=limit,
        )
        return results[0]

    async def read_many(
        self,
        *,
        candidate_ids: Sequence[int],
        actor_client_id: int,
        limit: int = _MAX_HISTORY,
    ) -> tuple[ChannelDMReplyLifecycleResult, ...]:
        normalized = list(dict.fromkeys(int(candidate_id) for candidate_id in candidate_ids))
        if not normalized or len(normalized) > _MAX_BATCH or any(candidate_id <= 0 for candidate_id in normalized):
            raise ChannelDMReplyLifecycleError(
                ChannelDMReplyLifecycleFailure.INVALID_REQUEST,
                "Channel-DM lifecycle candidate batch is invalid",
            )
        bounded_limit = min(max(int(limit), 1), _MAX_HISTORY)

        candidate_rows = (
            await self.session.execute(
                select(ContentCandidate).where(ContentCandidate.id.in_(normalized))
            )
        ).scalars().all()
        candidates = {int(candidate.id): candidate for candidate in candidate_rows}
        if set(candidates) != set(normalized):
            raise ChannelDMReplyLifecycleError(
                ChannelDMReplyLifecycleFailure.CANDIDATE_NOT_FOUND,
                "candidate not found",
            )

        document_ids = {int(candidate.source_document_id) for candidate in candidate_rows}
        channel_ids = {int(candidate.channel_id) for candidate in candidate_rows}
        document_rows = (
            await self.session.execute(
                select(SourceDocument).where(SourceDocument.id.in_(document_ids))
            )
        ).scalars().all()
        channel_rows = (
            await self.session.execute(select(Channel).where(Channel.id.in_(channel_ids)))
        ).scalars().all()
        documents = {int(document.id): document for document in document_rows}
        channels = {int(channel.id): channel for channel in channel_rows}

        for candidate_id in normalized:
            candidate = candidates[candidate_id]
            document = documents.get(int(candidate.source_document_id))
            channel = channels.get(int(candidate.channel_id))
            if document is None or channel is None or int(channel.owner_id) != int(actor_client_id):
                raise ChannelDMReplyLifecycleError(
                    ChannelDMReplyLifecycleFailure.CANDIDATE_NOT_FOUND,
                    "candidate not found",
                )
            if int(document.channel_id) != int(channel.id):
                raise ChannelDMReplyLifecycleError(
                    ChannelDMReplyLifecycleFailure.ROUTING_MISMATCH,
                    "candidate/source/channel binding disagrees",
                )
            metadata = _mapping(document.meta)
            if metadata is None or metadata.get("transport") != TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND:
                raise ChannelDMReplyLifecycleError(
                    ChannelDMReplyLifecycleFailure.ROUTING_MISMATCH,
                    "candidate is not an ordinary Telegram Channel DM",
                )

        ranked = (
            select(
                ChannelDMReplyCommand.id.label("command_id"),
                func.row_number()
                .over(
                    partition_by=ChannelDMReplyCommand.candidate_id,
                    order_by=(
                        ChannelDMReplyCommand.created_at.desc(),
                        ChannelDMReplyCommand.id.desc(),
                    ),
                )
                .label("history_rank"),
            )
            .where(ChannelDMReplyCommand.candidate_id.in_(normalized))
            .subquery()
        )
        command_rows = (
            await self.session.execute(
                select(ChannelDMReplyCommand)
                .join(ranked, ChannelDMReplyCommand.id == ranked.c.command_id)
                .where(ranked.c.history_rank <= bounded_limit)
                .order_by(
                    ChannelDMReplyCommand.candidate_id,
                    ChannelDMReplyCommand.created_at.desc(),
                    ChannelDMReplyCommand.id.desc(),
                )
            )
        ).scalars().all()
        grouped: dict[int, list[ChannelDMReplyLifecycleItem]] = {
            candidate_id: [] for candidate_id in normalized
        }
        for command in command_rows:
            candidate_id = int(command.candidate_id)
            candidate = candidates.get(candidate_id)
            if candidate is None:
                continue
            if int(command.source_document_id) != int(candidate.source_document_id):
                raise ChannelDMReplyLifecycleError(
                    ChannelDMReplyLifecycleFailure.MALFORMED_LIFECYCLE,
                    "stored Channel-DM command source binding is malformed",
                )
            grouped[candidate_id].append(_lifecycle_item(command))

        return tuple(
            ChannelDMReplyLifecycleResult(
                candidate_id=candidate_id,
                commands=tuple(grouped[candidate_id]),
            )
            for candidate_id in normalized
        )
