from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import ReplyParameters
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redaction import redact_secret_text
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceConnector, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.telegram_channel_dm_context import (
    ChannelDMContextResolver,
    ChannelDMContextRoutingError,
    channel_dm_external_id,
)
from app.services.telegram_channel_dms import TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND


_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_MAX_TEXT_LENGTH = 4096


class ChannelDMReplyFailure(str, Enum):
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    INVALID_REQUEST = "invalid_request"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    MALFORMED_PROVENANCE = "malformed_provenance"
    ROUTING_MISMATCH = "routing_mismatch"


class ChannelDMReplyError(RuntimeError):
    def __init__(self, failure: ChannelDMReplyFailure, message: str) -> None:
        self.failure = failure
        super().__init__(message)


class ChannelDMReplyBot(Protocol):
    async def get_chat(self, chat_id: int): ...
    async def get_me(self): ...
    async def get_chat_member(self, chat_id: int, user_id: int): ...
    async def send_message(
        self,
        *,
        chat_id: int,
        direct_messages_topic_id: int,
        text: str,
        reply_parameters: ReplyParameters,
        parse_mode: None,
    ): ...


@dataclass(frozen=True, slots=True)
class ChannelDMStoredReplyIdentity:
    direct_messages_chat_id: int
    direct_messages_topic_id: int
    inbound_message_id: int
    parent_chat_id: int


@dataclass(frozen=True, slots=True)
class ChannelDMReplyAuthority:
    candidate_id: int
    source_document_id: int
    connector_id: int
    channel_id: int
    parent_chat_id: int
    identity: ChannelDMStoredReplyIdentity


@dataclass(frozen=True, slots=True)
class ChannelDMReplyResult:
    command_id: int
    candidate_id: int
    state: str
    provider_message_id: int | None
    error_class: str | None
    reused_existing: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _normalize_request(reply_text: str, idempotency_key: str) -> tuple[str, str]:
    if not isinstance(reply_text, str) or not isinstance(idempotency_key, str):
        raise ChannelDMReplyError(ChannelDMReplyFailure.INVALID_REQUEST, "reply request is malformed")
    text = reply_text.strip()
    key = idempotency_key.strip()
    if not text:
        raise ChannelDMReplyError(ChannelDMReplyFailure.INVALID_REQUEST, "reply text is empty")
    if len(text) > _MAX_TEXT_LENGTH:
        raise ChannelDMReplyError(ChannelDMReplyFailure.INVALID_REQUEST, "reply text exceeds Telegram limit")
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise ChannelDMReplyError(ChannelDMReplyFailure.INVALID_REQUEST, "idempotency key is invalid")
    # RedactingBot applies the same transform before Telegram. Persist the effective
    # provider intent rather than retaining raw credential-shaped text in the ledger.
    return redact_secret_text(text), key


def _stored_identity(document: SourceDocument, channel: Channel) -> ChannelDMStoredReplyIdentity:
    metadata = _mapping(document.meta)
    if metadata is None or metadata.get("transport") != TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND:
        raise ChannelDMReplyError(
            ChannelDMReplyFailure.ROUTING_MISMATCH,
            "candidate is not an ordinary Telegram Channel DM",
        )
    topic = _mapping(metadata.get("telegram_direct_messages_topic"))
    dm_chat_id = _integer(metadata.get("telegram_direct_messages_chat_id"))
    message_id = _integer(metadata.get("telegram_message_id"))
    parent_chat_id = _integer(metadata.get("telegram_parent_chat_id"))
    topic_id = _integer(topic.get("topic_id")) if topic is not None else None
    if (
        dm_chat_id in (None, 0)
        or message_id is None
        or message_id <= 0
        or parent_chat_id in (None, 0)
        or topic_id is None
        or topic_id <= 0
    ):
        raise ChannelDMReplyError(
            ChannelDMReplyFailure.MALFORMED_PROVENANCE,
            "stored Channel-DM provenance is incomplete",
        )
    if int(parent_chat_id) != int(channel.tg_chat_id):
        raise ChannelDMReplyError(
            ChannelDMReplyFailure.ROUTING_MISMATCH,
            "stored Channel-DM parent disagrees with canonical Channel",
        )
    if str(document.external_id) != channel_dm_external_id(dm_chat_id, message_id):
        raise ChannelDMReplyError(
            ChannelDMReplyFailure.MALFORMED_PROVENANCE,
            "stored native identity disagrees with source identity",
        )
    return ChannelDMStoredReplyIdentity(
        direct_messages_chat_id=dm_chat_id,
        direct_messages_topic_id=topic_id,
        inbound_message_id=message_id,
        parent_chat_id=parent_chat_id,
    )


def _result(command: ChannelDMReplyCommand, *, reused_existing: bool) -> ChannelDMReplyResult:
    return ChannelDMReplyResult(
        command_id=int(command.id),
        candidate_id=int(command.candidate_id),
        state=str(command.state),
        provider_message_id=(
            int(command.provider_message_id) if command.provider_message_id is not None else None
        ),
        error_class=command.error_class,
        reused_existing=reused_existing,
    )


class ChannelDMReplyService:
    """Durable explicit text replies to ordinary Telegram Channel DMs.

    The globally unique command key is committed before the non-idempotent Telegram
    call. Only the request that creates the row may dispatch. Any existing row
    (including pending/uncertain) is a no-resend evidence barrier for that key.
    """

    def __init__(self, session: AsyncSession, *, bot: ChannelDMReplyBot) -> None:
        self.session = session
        self.bot = bot
        self.sources = SourcesRepo(session)
        self.context = ChannelDMContextResolver(session, bot=bot)

    async def _load_binding(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
    ) -> tuple[ContentCandidate, SourceDocument, SourceConnector, Channel]:
        candidate = (
            await self.session.execute(
                select(ContentCandidate)
                .where(ContentCandidate.id == int(candidate_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise ChannelDMReplyError(ChannelDMReplyFailure.CANDIDATE_NOT_FOUND, "candidate not found")
        document = await self.session.get(SourceDocument, int(candidate.source_document_id))
        connector = (
            await self.session.get(SourceConnector, int(document.connector_id))
            if document is not None
            else None
        )
        channel = (
            await self.session.get(Channel, int(connector.channel_id))
            if connector is not None
            else None
        )
        if document is None or connector is None:
            raise ChannelDMReplyError(
                ChannelDMReplyFailure.MALFORMED_PROVENANCE,
                "candidate source authority is incomplete",
            )
        if channel is None or int(channel.owner_id) != int(actor_client_id):
            raise ChannelDMReplyError(ChannelDMReplyFailure.CANDIDATE_NOT_FOUND, "candidate not found")
        if not (
            int(candidate.channel_id)
            == int(document.channel_id)
            == int(connector.channel_id)
            == int(channel.id)
        ):
            raise ChannelDMReplyError(
                ChannelDMReplyFailure.ROUTING_MISMATCH,
                "candidate/source/connector routing disagrees",
            )
        return candidate, document, connector, channel

    async def _authority_for_new_command(
        self,
        *,
        candidate: ContentCandidate,
        document: SourceDocument,
        connector: SourceConnector,
        channel: Channel,
    ) -> ChannelDMReplyAuthority:
        if not connector.enabled or str(connector.kind) != TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND:
            raise ChannelDMReplyError(
                ChannelDMReplyFailure.ROUTING_MISMATCH,
                "ordinary Channel-DM connector is no longer trusted",
            )
        if str(connector.value) != str(int(channel.tg_chat_id)):
            raise ChannelDMReplyError(
                ChannelDMReplyFailure.ROUTING_MISMATCH,
                "connector Telegram channel disagrees with canonical Channel",
            )
        identity = _stored_identity(document, channel)
        matches = [
            row
            for row in await self.sources.list_connectors(int(channel.id))
            if row.enabled
            and str(row.kind) == TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND
            and str(row.value) == str(int(channel.tg_chat_id))
        ]
        if len(matches) != 1 or int(matches[0].id) != int(connector.id):
            raise ChannelDMReplyError(
                ChannelDMReplyFailure.ROUTING_MISMATCH,
                "trusted Channel-DM connector mapping is not unique",
            )
        return ChannelDMReplyAuthority(
            candidate_id=int(candidate.id),
            source_document_id=int(document.id),
            connector_id=int(connector.id),
            channel_id=int(channel.id),
            parent_chat_id=int(channel.tg_chat_id),
            identity=identity,
        )

    async def _existing_command(
        self,
        *,
        idempotency_key: str,
    ) -> ChannelDMReplyCommand | None:
        return (
            await self.session.execute(
                select(ChannelDMReplyCommand)
                .where(ChannelDMReplyCommand.idempotency_key == idempotency_key)
                .with_for_update()
            )
        ).scalar_one_or_none()

    @staticmethod
    def _assert_existing_matches(
        command: ChannelDMReplyCommand,
        *,
        candidate_id: int,
        source_document_id: int,
        reply_text: str,
    ) -> None:
        if (
            int(command.candidate_id) != int(candidate_id)
            or int(command.source_document_id) != int(source_document_id)
            or str(command.reply_text) != reply_text
        ):
            raise ChannelDMReplyError(
                ChannelDMReplyFailure.IDEMPOTENCY_CONFLICT,
                "idempotency key is already bound to a different reply intent",
            )

    async def _reserve(
        self,
        *,
        authority: ChannelDMReplyAuthority,
        reply_text: str,
        idempotency_key: str,
    ) -> tuple[ChannelDMReplyCommand, bool]:
        # Recheck after authority derivation so SQLite/no-op FOR UPDATE and any future
        # caller still receive the globally unique-ledger race guarantee.
        existing = await self._existing_command(idempotency_key=idempotency_key)
        if existing is not None:
            self._assert_existing_matches(
                existing,
                candidate_id=authority.candidate_id,
                source_document_id=authority.source_document_id,
                reply_text=reply_text,
            )
            await self.session.commit()
            return existing, True

        command = ChannelDMReplyCommand(
            candidate_id=authority.candidate_id,
            source_document_id=authority.source_document_id,
            idempotency_key=idempotency_key,
            reply_text=reply_text,
            state="pending",
        )
        self.session.add(command)
        try:
            await self.session.commit()
            await self.session.refresh(command)
            return command, False
        except IntegrityError:
            await self.session.rollback()
            winner = await self._existing_command(idempotency_key=idempotency_key)
            if winner is None:
                raise
            self._assert_existing_matches(
                winner,
                candidate_id=authority.candidate_id,
                source_document_id=authority.source_document_id,
                reply_text=reply_text,
            )
            await self.session.commit()
            return winner, True

    async def _mark_terminal(
        self,
        command_id: int,
        *,
        state: str,
        provider_message_id: int | None = None,
        error_class: str | None = None,
    ) -> ChannelDMReplyCommand:
        now = _utcnow()
        await self.session.execute(
            update(ChannelDMReplyCommand)
            .where(
                ChannelDMReplyCommand.id == int(command_id),
                ChannelDMReplyCommand.state == "pending",
            )
            .values(
                state=state,
                provider_message_id=provider_message_id,
                error_class=error_class,
                finished_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.commit()
        command = await self.session.get(ChannelDMReplyCommand, int(command_id))
        assert command is not None
        return command

    async def _mark_dispatch_started(self, command_id: int) -> bool:
        now = _utcnow()
        result = await self.session.execute(
            update(ChannelDMReplyCommand)
            .where(
                ChannelDMReplyCommand.id == int(command_id),
                ChannelDMReplyCommand.state == "pending",
                ChannelDMReplyCommand.dispatch_started_at.is_(None),
            )
            .values(dispatch_started_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await self.session.commit()
        return int(result.rowcount or 0) == 1

    async def _fresh_action_checks(self, authority: ChannelDMReplyAuthority) -> str | None:
        try:
            context = await self.context.resolve(
                direct_messages_chat_id=authority.identity.direct_messages_chat_id,
                connector_kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
            )
        except ChannelDMContextRoutingError:
            return "routing_mismatch"
        except Exception:
            return "routing_check_unavailable"
        if (
            int(context.channel_id) != authority.channel_id
            or int(context.parent_chat_id) != authority.parent_chat_id
            or int(context.connector.id) != authority.connector_id
        ):
            return "routing_mismatch"
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(authority.parent_chat_id, int(me.id))
        except Exception:
            return "rights_check_unavailable"
        if getattr(member, "can_manage_direct_messages", None) is not True:
            return "insufficient_rights"
        return None

    @staticmethod
    def _provider_error_class(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, TelegramForbiddenError):
            return "failed", "provider_forbidden"
        if isinstance(exc, TelegramNotFound):
            return "failed", "native_reply_target_missing"
        if isinstance(exc, TelegramBadRequest):
            return "failed", "provider_rejected"
        if isinstance(exc, TelegramRetryAfter):
            return "failed", "provider_retry_after"
        if isinstance(exc, (TelegramNetworkError, TelegramServerError)):
            return "uncertain", "provider_outcome_unknown"
        if isinstance(exc, TelegramAPIError):
            return "uncertain", "provider_outcome_unknown"
        return "uncertain", "provider_outcome_unknown"

    async def execute(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
        reply_text: str,
        idempotency_key: str,
    ) -> ChannelDMReplyResult:
        text, key = _normalize_request(reply_text, idempotency_key)
        candidate, document, connector, channel = await self._load_binding(
            candidate_id=candidate_id,
            actor_client_id=actor_client_id,
        )

        # Existing command state is durable delivery evidence. After authenticating
        # the actor and immutable candidate/source binding, return it before mutable
        # connector/provenance/Telegram checks. This guarantees duplicate HTTP calls
        # never turn a late connector disable or routing drift into a second send.
        existing = await self._existing_command(idempotency_key=key)
        if existing is not None:
            self._assert_existing_matches(
                existing,
                candidate_id=int(candidate.id),
                source_document_id=int(document.id),
                reply_text=text,
            )
            await self.session.commit()
            return _result(existing, reused_existing=True)

        authority = await self._authority_for_new_command(
            candidate=candidate,
            document=document,
            connector=connector,
            channel=channel,
        )
        command, reused = await self._reserve(
            authority=authority,
            reply_text=text,
            idempotency_key=key,
        )
        if reused:
            return _result(command, reused_existing=True)

        pre_dispatch_failure = await self._fresh_action_checks(authority)
        if pre_dispatch_failure is not None:
            command = await self._mark_terminal(
                int(command.id), state="failed", error_class=pre_dispatch_failure
            )
            return _result(command, reused_existing=False)

        if not await self._mark_dispatch_started(int(command.id)):
            command = await self.session.get(ChannelDMReplyCommand, int(command.id))
            assert command is not None
            return _result(command, reused_existing=True)

        try:
            message = await self.bot.send_message(
                chat_id=authority.identity.direct_messages_chat_id,
                direct_messages_topic_id=authority.identity.direct_messages_topic_id,
                text=text,
                reply_parameters=ReplyParameters(
                    message_id=authority.identity.inbound_message_id,
                ),
                parse_mode=None,
            )
        except Exception as exc:
            state, error_class = self._provider_error_class(exc)
            command = await self._mark_terminal(
                int(command.id), state=state, error_class=error_class
            )
            return _result(command, reused_existing=False)

        provider_message_id = _integer(getattr(message, "message_id", None))
        if provider_message_id is None or provider_message_id <= 0:
            command = await self._mark_terminal(
                int(command.id),
                state="uncertain",
                error_class="provider_confirmation_malformed",
            )
        else:
            command = await self._mark_terminal(
                int(command.id), state="sent", provider_message_id=provider_message_id
            )
        return _result(command, reused_existing=False)
