from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redaction import redact_secret_text
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.channel_dm_reply_intent import ChannelDMReplyIntent
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.services.channel_dm_replies import (
    ChannelDMReplyBot,
    ChannelDMReplyError,
    ChannelDMReplyResult,
    ChannelDMReplyService,
)
from app.services.channel_dm_reply_proposals import (
    ChannelDMReplyProposalContext,
    ChannelDMReplyProposalError,
    ChannelDMReplyProposalFailure,
    ChannelDMReplyProposalService,
)
from app.services.telegram_channel_dms import TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND


_MAX_REPLY_TEXT_CHARS = 4096
_MAX_HISTORY = 10
_MAX_BATCH = 100


class ChannelDMReplyIntentFailure(str, Enum):
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    INTENT_NOT_FOUND = "intent_not_found"
    INVALID_REQUEST = "invalid_request"
    ROUTING_MISMATCH = "routing_mismatch"
    STALE = "stale"
    NOT_REVIEWABLE = "not_reviewable"
    PROPOSAL_UNAVAILABLE = "proposal_unavailable"
    HANDOFF_REJECTED = "handoff_rejected"
    MALFORMED_INTENT = "malformed_intent"


class ChannelDMReplyIntentError(RuntimeError):
    def __init__(self, failure: ChannelDMReplyIntentFailure, message: str) -> None:
        self.failure = failure
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ChannelDMReplyIntentView:
    intent_id: int
    candidate_id: int
    reply_text: str
    origin: str
    state: str
    is_current: bool
    handoff_in_progress: bool
    consumed_command_id: int | None
    created_at: datetime
    updated_at: datetime
    consumed_at: datetime | None
    dismissed_at: datetime | None
    stale_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChannelDMReplyIntentList:
    candidate_id: int
    intents: tuple[ChannelDMReplyIntentView, ...]


@dataclass(frozen=True, slots=True)
class ChannelDMReplyIntentWriteResult:
    intent: ChannelDMReplyIntentView
    reused_existing: bool


@dataclass(frozen=True, slots=True)
class ChannelDMReplyIntentSendResult:
    intent: ChannelDMReplyIntentView
    command: ChannelDMReplyResult


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _normalize_reply_text(value: str) -> str:
    if not isinstance(value, str):
        raise ChannelDMReplyIntentError(
            ChannelDMReplyIntentFailure.INVALID_REQUEST,
            "reply intent text is malformed",
        )
    text = redact_secret_text(value.strip())
    if not text or len(text) > _MAX_REPLY_TEXT_CHARS:
        raise ChannelDMReplyIntentError(
            ChannelDMReplyIntentFailure.INVALID_REQUEST,
            "reply intent text is invalid",
        )
    return text


def _new_handoff_key() -> str:
    # Allocated only after an explicit user Send. It is never exposed in the
    # browser DTO and cannot be selected by AI/automation/proposal creation.
    return f"dm-reply-intent:{uuid4().hex}"


def _handoff_key(intent: ChannelDMReplyIntent) -> str:
    key = str(intent.handoff_idempotency_key or "").strip()
    if intent.handoff_started_at is None or not key:
        raise ChannelDMReplyIntentError(
            ChannelDMReplyIntentFailure.MALFORMED_INTENT,
            "reply intent handoff identity is malformed",
        )
    return key


def _proposal_failure(exc: ChannelDMReplyProposalError) -> ChannelDMReplyIntentFailure:
    if exc.failure == ChannelDMReplyProposalFailure.CANDIDATE_NOT_FOUND:
        return ChannelDMReplyIntentFailure.CANDIDATE_NOT_FOUND
    if exc.failure in {
        ChannelDMReplyProposalFailure.ROUTING_MISMATCH,
        ChannelDMReplyProposalFailure.MALFORMED_PROVENANCE,
    }:
        return ChannelDMReplyIntentFailure.ROUTING_MISMATCH
    return ChannelDMReplyIntentFailure.PROPOSAL_UNAVAILABLE


def _mark_stale(intent: ChannelDMReplyIntent, *, now: datetime) -> None:
    intent.state = "stale"
    intent.active_slot = None
    intent.stale_at = now
    intent.updated_at = now


def _mark_consumed(
    intent: ChannelDMReplyIntent,
    *,
    command_id: int,
    now: datetime,
) -> None:
    intent.state = "consumed"
    intent.active_slot = None
    intent.consumed_command_id = int(command_id)
    intent.consumed_at = now
    intent.updated_at = now


def _project(intent: ChannelDMReplyIntent, *, current_hash: str | None) -> ChannelDMReplyIntentView:
    state = str(intent.state)
    handoff = intent.handoff_started_at is not None and state == "pending_review"
    return ChannelDMReplyIntentView(
        intent_id=int(intent.id),
        candidate_id=int(intent.candidate_id),
        reply_text=str(intent.proposed_text),
        origin=str(intent.origin),
        state=state,
        is_current=(
            state == "pending_review"
            and not handoff
            and current_hash is not None
            and str(intent.source_content_hash) == str(current_hash)
        ),
        handoff_in_progress=handoff,
        consumed_command_id=(
            int(intent.consumed_command_id)
            if intent.consumed_command_id is not None
            else None
        ),
        created_at=intent.created_at,
        updated_at=intent.updated_at,
        consumed_at=intent.consumed_at,
        dismissed_at=intent.dismissed_at,
        stale_at=intent.stale_at,
    )


def _command_result(command: ChannelDMReplyCommand) -> ChannelDMReplyResult:
    return ChannelDMReplyResult(
        command_id=int(command.id),
        candidate_id=int(command.candidate_id),
        state=str(command.state),
        provider_message_id=(
            int(command.provider_message_id) if command.provider_message_id is not None else None
        ),
        error_class=command.error_class,
        reused_existing=True,
    )


def _assert_command_binding(
    intent: ChannelDMReplyIntent,
    command: ChannelDMReplyCommand,
) -> None:
    if (
        int(command.candidate_id) != int(intent.candidate_id)
        or int(command.source_document_id) != int(intent.source_document_id)
        or str(command.reply_text) != str(intent.proposed_text)
    ):
        raise ChannelDMReplyIntentError(
            ChannelDMReplyIntentFailure.MALFORMED_INTENT,
            "reply intent command binding is malformed",
        )
    if intent.handoff_idempotency_key is not None and (
        str(command.idempotency_key) != str(intent.handoff_idempotency_key)
    ):
        raise ChannelDMReplyIntentError(
            ChannelDMReplyIntentFailure.MALFORMED_INTENT,
            "reply intent command identity is malformed",
        )


class ChannelDMReplyIntentService:
    """Durable proposal/review authority; T5.3 remains the only send authority."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        bot: ChannelDMReplyBot,
        proposal_service: ChannelDMReplyProposalService | None = None,
    ) -> None:
        self.session = session
        self.bot = bot
        self.proposals = proposal_service or ChannelDMReplyProposalService(session)

    async def _load_owned_history_binding(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
        for_update: bool = False,
    ) -> tuple[ContentCandidate, SourceDocument, Channel]:
        candidate_stmt = select(ContentCandidate).where(
            ContentCandidate.id == int(candidate_id)
        )
        if for_update:
            candidate_stmt = candidate_stmt.with_for_update()
        candidate = (await self.session.execute(candidate_stmt)).scalar_one_or_none()
        if candidate is None:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.CANDIDATE_NOT_FOUND,
                "candidate not found",
            )

        document_stmt = select(SourceDocument).where(
            SourceDocument.id == int(candidate.source_document_id)
        )
        if for_update:
            document_stmt = document_stmt.with_for_update()
        document = (await self.session.execute(document_stmt)).scalar_one_or_none()
        channel = await self.session.get(Channel, int(candidate.channel_id))
        if document is None or channel is None or int(channel.owner_id) != int(actor_client_id):
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.CANDIDATE_NOT_FOUND,
                "candidate not found",
            )
        if int(document.channel_id) != int(channel.id):
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.ROUTING_MISMATCH,
                "candidate/source/channel binding disagrees",
            )
        metadata = _mapping(document.meta)
        if metadata is None or metadata.get("transport") != TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.ROUTING_MISMATCH,
                "candidate is not an ordinary Channel DM",
            )
        return candidate, document, channel

    async def _lock_intent(
        self,
        *,
        intent_id: int,
        actor_client_id: int,
    ) -> ChannelDMReplyIntent:
        intent = (
            await self.session.execute(
                select(ChannelDMReplyIntent)
                .where(ChannelDMReplyIntent.id == int(intent_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if intent is None or int(intent.owner_client_id) != int(actor_client_id):
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.INTENT_NOT_FOUND,
                "reply intent not found",
            )
        return intent

    async def _active_intent(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
    ) -> ChannelDMReplyIntent | None:
        return (
            await self.session.execute(
                select(ChannelDMReplyIntent)
                .where(
                    ChannelDMReplyIntent.candidate_id == int(candidate_id),
                    ChannelDMReplyIntent.owner_client_id == int(actor_client_id),
                    ChannelDMReplyIntent.active_slot == 1,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _lock_current_source(
        self,
        *,
        intent: ChannelDMReplyIntent,
        actor_client_id: int,
    ) -> tuple[ContentCandidate, SourceDocument]:
        candidate, document, _ = await self._load_owned_history_binding(
            candidate_id=int(intent.candidate_id),
            actor_client_id=actor_client_id,
            for_update=True,
        )
        if int(candidate.source_document_id) != int(intent.source_document_id):
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                "reply intent source binding is malformed",
            )
        return candidate, document

    async def _existing_handoff_command(
        self,
        intent: ChannelDMReplyIntent,
    ) -> ChannelDMReplyCommand | None:
        key = _handoff_key(intent)
        command = (
            await self.session.execute(
                select(ChannelDMReplyCommand).where(
                    ChannelDMReplyCommand.idempotency_key == key
                )
            )
        ).scalar_one_or_none()
        if command is not None:
            _assert_command_binding(intent, command)
        return command

    @staticmethod
    def _guard_automation_replacement(
        active: ChannelDMReplyIntent,
        *,
        origin: str,
    ) -> None:
        if origin == "automation" and str(active.origin) != "automation":
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.NOT_REVIEWABLE,
                "automation cannot replace an active human/AI review intent",
            )

    async def _upsert_current(
        self,
        *,
        context: ChannelDMReplyProposalContext,
        actor_client_id: int,
        reply_text: str,
        origin: str,
        generation_meta: Mapping[str, Any] | None = None,
    ) -> ChannelDMReplyIntentWriteResult:
        text = _normalize_reply_text(reply_text)
        if origin not in {"manual", "ai", "automation"}:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.INVALID_REQUEST,
                "reply intent origin is invalid",
            )

        candidate, document, _ = await self._load_owned_history_binding(
            candidate_id=context.candidate_id,
            actor_client_id=actor_client_id,
            for_update=True,
        )
        if (
            int(candidate.source_document_id) != int(context.source_document_id)
            or int(document.id) != int(context.source_document_id)
        ):
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.ROUTING_MISMATCH,
                "proposal source binding changed before persistence",
            )
        if str(document.content_hash) != str(context.source_content_hash):
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.STALE,
                "source changed while the proposal was being prepared",
            )

        now = _utcnow()
        active = await self._active_intent(
            candidate_id=int(candidate.id),
            actor_client_id=actor_client_id,
        )
        if active is not None and str(active.source_content_hash) != str(document.content_hash):
            if active.handoff_started_at is not None:
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.NOT_REVIEWABLE,
                    "previous reply intent handoff is already in progress",
                )
            _mark_stale(active, now=now)
            await self.session.flush()
            active = None

        if active is not None:
            if active.handoff_started_at is not None:
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.NOT_REVIEWABLE,
                    "reply intent handoff is already in progress",
                )
            self._guard_automation_replacement(active, origin=origin)
            active.proposed_text = text
            active.origin = origin
            active.generation_meta = dict(generation_meta or {})
            active.updated_at = now
            await self.session.commit()
            await self.session.refresh(active)
            return ChannelDMReplyIntentWriteResult(
                intent=_project(active, current_hash=str(document.content_hash)),
                reused_existing=True,
            )

        intent = ChannelDMReplyIntent(
            candidate_id=int(candidate.id),
            source_document_id=int(document.id),
            owner_client_id=int(actor_client_id),
            proposed_text=text,
            source_content_hash=str(document.content_hash),
            origin=origin,
            state="pending_review",
            active_slot=1,
            generation_meta=dict(generation_meta or {}),
        )
        self.session.add(intent)
        try:
            await self.session.commit()
            await self.session.refresh(intent)
            return ChannelDMReplyIntentWriteResult(
                intent=_project(intent, current_hash=str(document.content_hash)),
                reused_existing=False,
            )
        except IntegrityError:
            await self.session.rollback()
            candidate, document, _ = await self._load_owned_history_binding(
                candidate_id=context.candidate_id,
                actor_client_id=actor_client_id,
                for_update=True,
            )
            winner = await self._active_intent(
                candidate_id=int(candidate.id),
                actor_client_id=actor_client_id,
            )
            if winner is None:
                raise
            if (
                int(winner.source_document_id) != int(document.id)
                or str(winner.source_content_hash) != str(document.content_hash)
                or winner.handoff_started_at is not None
            ):
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.NOT_REVIEWABLE,
                    "concurrent reply intent is not safely replaceable",
                )
            self._guard_automation_replacement(winner, origin=origin)
            winner.proposed_text = text
            winner.origin = origin
            winner.generation_meta = dict(generation_meta or {})
            winner.updated_at = _utcnow()
            await self.session.commit()
            await self.session.refresh(winner)
            return ChannelDMReplyIntentWriteResult(
                intent=_project(winner, current_hash=str(document.content_hash)),
                reused_existing=True,
            )

    async def create_manual(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
        reply_text: str,
    ) -> ChannelDMReplyIntentWriteResult:
        try:
            context = await self.proposals.load_context(
                candidate_id=candidate_id,
                actor_client_id=actor_client_id,
            )
        except ChannelDMReplyProposalError as exc:
            raise ChannelDMReplyIntentError(
                _proposal_failure(exc),
                "ordinary Channel-DM proposal context is unavailable",
            ) from exc
        return await self._upsert_current(
            context=context,
            actor_client_id=actor_client_id,
            reply_text=reply_text,
            origin="manual",
            generation_meta={"source": "studio_manual"},
        )

    async def create_ai(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
        origin: str = "ai",
    ) -> ChannelDMReplyIntentWriteResult:
        if origin not in {"ai", "automation"}:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.INVALID_REQUEST,
                "AI reply intent origin is invalid",
            )
        try:
            draft = await self.proposals.propose_draft(
                candidate_id=candidate_id,
                actor_client_id=actor_client_id,
            )
        except ChannelDMReplyProposalError as exc:
            raise ChannelDMReplyIntentError(
                _proposal_failure(exc),
                "AI reply proposal is unavailable",
            ) from exc
        return await self._upsert_current(
            context=draft.context,
            actor_client_id=actor_client_id,
            reply_text=draft.reply_text,
            origin=origin,
            generation_meta={"proposal_authority": "t5.4"},
        )

    async def read_many(
        self,
        *,
        candidate_ids: Sequence[int],
        actor_client_id: int,
        limit: int = _MAX_HISTORY,
    ) -> tuple[ChannelDMReplyIntentList, ...]:
        normalized = list(dict.fromkeys(int(value) for value in candidate_ids))
        if not normalized or len(normalized) > _MAX_BATCH or any(value <= 0 for value in normalized):
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.INVALID_REQUEST,
                "reply intent candidate batch is invalid",
            )
        bounded_limit = min(max(int(limit), 1), _MAX_HISTORY)

        candidate_rows = (
            await self.session.execute(
                select(ContentCandidate).where(ContentCandidate.id.in_(normalized))
            )
        ).scalars().all()
        candidates = {int(row.id): row for row in candidate_rows}
        if set(candidates) != set(normalized):
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.CANDIDATE_NOT_FOUND,
                "candidate not found",
            )
        document_ids = {int(row.source_document_id) for row in candidate_rows}
        channel_ids = {int(row.channel_id) for row in candidate_rows}
        documents = {
            int(row.id): row
            for row in (
                await self.session.execute(
                    select(SourceDocument).where(SourceDocument.id.in_(document_ids))
                )
            ).scalars().all()
        }
        channels = {
            int(row.id): row
            for row in (
                await self.session.execute(select(Channel).where(Channel.id.in_(channel_ids)))
            ).scalars().all()
        }
        for candidate_id in normalized:
            candidate = candidates[candidate_id]
            document = documents.get(int(candidate.source_document_id))
            channel = channels.get(int(candidate.channel_id))
            if document is None or channel is None or int(channel.owner_id) != int(actor_client_id):
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.CANDIDATE_NOT_FOUND,
                    "candidate not found",
                )
            metadata = _mapping(document.meta)
            if (
                int(document.channel_id) != int(channel.id)
                or metadata is None
                or metadata.get("transport") != TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND
            ):
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.ROUTING_MISMATCH,
                    "candidate is not an ordinary Channel DM",
                )

        ranked = (
            select(
                ChannelDMReplyIntent.id.label("intent_id"),
                func.row_number()
                .over(
                    partition_by=ChannelDMReplyIntent.candidate_id,
                    order_by=(
                        ChannelDMReplyIntent.created_at.desc(),
                        ChannelDMReplyIntent.id.desc(),
                    ),
                )
                .label("history_rank"),
            )
            .where(
                ChannelDMReplyIntent.candidate_id.in_(normalized),
                ChannelDMReplyIntent.owner_client_id == int(actor_client_id),
            )
            .subquery()
        )
        intents = (
            await self.session.execute(
                select(ChannelDMReplyIntent)
                .join(ranked, ChannelDMReplyIntent.id == ranked.c.intent_id)
                .where(ranked.c.history_rank <= bounded_limit)
                .order_by(
                    ChannelDMReplyIntent.candidate_id,
                    ChannelDMReplyIntent.created_at.desc(),
                    ChannelDMReplyIntent.id.desc(),
                )
            )
        ).scalars().all()

        handoff_keys = {
            _handoff_key(intent)
            for intent in intents
            if str(intent.state) == "pending_review"
            and intent.handoff_started_at is not None
            and intent.consumed_command_id is None
        }
        commands_by_key: dict[str, ChannelDMReplyCommand] = {}
        if handoff_keys:
            command_rows = (
                await self.session.execute(
                    select(ChannelDMReplyCommand).where(
                        ChannelDMReplyCommand.idempotency_key.in_(list(handoff_keys))
                    )
                )
            ).scalars().all()
            commands_by_key = {str(row.idempotency_key): row for row in command_rows}

        changed = False
        now = _utcnow()
        for intent in intents:
            candidate = candidates[int(intent.candidate_id)]
            document = documents[int(candidate.source_document_id)]
            if int(intent.source_document_id) != int(document.id):
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                    "reply intent source binding is malformed",
                )
            if str(intent.state) != "pending_review":
                continue
            if intent.handoff_started_at is not None:
                key = _handoff_key(intent)
                command = commands_by_key.get(key)
                if command is None:
                    raise ChannelDMReplyIntentError(
                        ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                        "durable reply intent handoff has no reserved command",
                    )
                _assert_command_binding(intent, command)
                _mark_consumed(intent, command_id=int(command.id), now=now)
                changed = True
                continue
            if intent.handoff_idempotency_key is not None:
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                    "reply intent has a pre-send handoff identity",
                )
            if str(intent.source_content_hash) != str(document.content_hash):
                _mark_stale(intent, now=now)
                changed = True
        if changed:
            await self.session.commit()

        grouped: dict[int, list[ChannelDMReplyIntentView]] = {
            candidate_id: [] for candidate_id in normalized
        }
        for intent in intents:
            candidate = candidates[int(intent.candidate_id)]
            document = documents[int(candidate.source_document_id)]
            grouped[int(intent.candidate_id)].append(
                _project(intent, current_hash=str(document.content_hash))
            )
        return tuple(
            ChannelDMReplyIntentList(
                candidate_id=candidate_id,
                intents=tuple(grouped[candidate_id]),
            )
            for candidate_id in normalized
        )

    async def read(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
    ) -> ChannelDMReplyIntentList:
        return (
            await self.read_many(
                candidate_ids=[candidate_id],
                actor_client_id=actor_client_id,
            )
        )[0]

    async def edit(
        self,
        *,
        intent_id: int,
        actor_client_id: int,
        reply_text: str,
    ) -> ChannelDMReplyIntentView:
        text = _normalize_reply_text(reply_text)
        intent = await self._lock_intent(
            intent_id=intent_id,
            actor_client_id=actor_client_id,
        )
        if str(intent.state) != "pending_review" or intent.handoff_started_at is not None:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.NOT_REVIEWABLE,
                "reply intent is no longer editable",
            )
        if intent.handoff_idempotency_key is not None:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                "reply intent has a pre-send handoff identity",
            )
        _, document = await self._lock_current_source(
            intent=intent,
            actor_client_id=actor_client_id,
        )
        if str(intent.source_content_hash) != str(document.content_hash):
            now = _utcnow()
            _mark_stale(intent, now=now)
            await self.session.commit()
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.STALE,
                "reply intent source has changed",
            )
        intent.proposed_text = text
        intent.updated_at = _utcnow()
        await self.session.commit()
        await self.session.refresh(intent)
        return _project(intent, current_hash=str(document.content_hash))

    async def dismiss(
        self,
        *,
        intent_id: int,
        actor_client_id: int,
    ) -> ChannelDMReplyIntentView:
        intent = await self._lock_intent(
            intent_id=intent_id,
            actor_client_id=actor_client_id,
        )
        if str(intent.state) != "pending_review" or intent.handoff_started_at is not None:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.NOT_REVIEWABLE,
                "reply intent is no longer dismissible",
            )
        if intent.handoff_idempotency_key is not None:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                "reply intent has a pre-send handoff identity",
            )
        _, document = await self._lock_current_source(
            intent=intent,
            actor_client_id=actor_client_id,
        )
        if str(intent.source_content_hash) != str(document.content_hash):
            now = _utcnow()
            _mark_stale(intent, now=now)
            await self.session.commit()
            return _project(intent, current_hash=str(document.content_hash))
        now = _utcnow()
        intent.state = "dismissed"
        intent.active_slot = None
        intent.dismissed_at = now
        intent.updated_at = now
        await self.session.commit()
        await self.session.refresh(intent)
        return _project(intent, current_hash=str(document.content_hash))

    async def send(
        self,
        *,
        intent_id: int,
        actor_client_id: int,
    ) -> ChannelDMReplyIntentSendResult:
        intent = await self._lock_intent(
            intent_id=intent_id,
            actor_client_id=actor_client_id,
        )
        if str(intent.state) == "consumed" and intent.consumed_command_id is not None:
            command = await self.session.get(
                ChannelDMReplyCommand,
                int(intent.consumed_command_id),
            )
            if command is None:
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                    "consumed reply intent lost its command linkage",
                )
            _assert_command_binding(intent, command)
            _, document = await self._lock_current_source(
                intent=intent,
                actor_client_id=actor_client_id,
            )
            return ChannelDMReplyIntentSendResult(
                intent=_project(intent, current_hash=str(document.content_hash)),
                command=_command_result(command),
            )
        if str(intent.state) != "pending_review":
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.NOT_REVIEWABLE,
                "reply intent cannot be consumed",
            )

        _, document = await self._lock_current_source(
            intent=intent,
            actor_client_id=actor_client_id,
        )
        if intent.handoff_started_at is not None:
            command = await self._existing_handoff_command(intent)
            if command is None:
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                    "durable reply intent handoff has no reserved command",
                )
            _mark_consumed(intent, command_id=int(command.id), now=_utcnow())
            await self.session.commit()
            await self.session.refresh(intent)
            return ChannelDMReplyIntentSendResult(
                intent=_project(intent, current_hash=str(document.content_hash)),
                command=_command_result(command),
            )
        if intent.handoff_idempotency_key is not None:
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                "reply intent has a pre-send handoff identity",
            )

        if str(intent.source_content_hash) != str(document.content_hash):
            now = _utcnow()
            _mark_stale(intent, now=now)
            await self.session.commit()
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.STALE,
                "reply intent source has changed",
            )

        intent.handoff_started_at = _utcnow()
        intent.handoff_idempotency_key = _new_handoff_key()
        intent.updated_at = intent.handoff_started_at
        handoff_key = _handoff_key(intent)
        # T5.3's first command reservation commits the handoff fence + random key
        # together with the globally unique command before any provider mutation.
        await self.session.flush()

        try:
            command = await ChannelDMReplyService(self.session, bot=self.bot).execute(
                candidate_id=int(intent.candidate_id),
                actor_client_id=int(actor_client_id),
                reply_text=str(intent.proposed_text),
                idempotency_key=handoff_key,
            )
        except ChannelDMReplyError as exc:
            await self.session.rollback()
            raise ChannelDMReplyIntentError(
                ChannelDMReplyIntentFailure.HANDOFF_REJECTED,
                "T5.3 reply command handoff was rejected",
            ) from exc

        locked = await self._lock_intent(
            intent_id=int(intent.id),
            actor_client_id=actor_client_id,
        )
        if str(locked.state) == "consumed":
            if int(locked.consumed_command_id or 0) != int(command.command_id):
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                    "reply intent was consumed by a different command",
                )
        else:
            if str(locked.state) != "pending_review" or locked.handoff_started_at is None:
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                    "reply intent handoff state changed unexpectedly",
                )
            if str(locked.handoff_idempotency_key or "") != handoff_key:
                raise ChannelDMReplyIntentError(
                    ChannelDMReplyIntentFailure.MALFORMED_INTENT,
                    "reply intent handoff identity changed unexpectedly",
                )
            _mark_consumed(locked, command_id=command.command_id, now=_utcnow())
            await self.session.commit()
            await self.session.refresh(locked)
        current_document = await self.session.get(SourceDocument, int(locked.source_document_id))
        current_hash = str(current_document.content_hash) if current_document is not None else None
        return ChannelDMReplyIntentSendResult(
            intent=_project(locked, current_hash=current_hash),
            command=command,
        )
