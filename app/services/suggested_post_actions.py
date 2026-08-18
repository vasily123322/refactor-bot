from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceConnector, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_reconciliation import (
    SourceIngestionReconciliationService,
    SourceProjection,
    SourceProjectionUpdateMode,
)
from app.services.suggested_post_business import (
    effective_native_state,
    integer,
    mapping,
    paid_proposal_is_safe_to_approve,
    text,
)
from app.services.telegram_suggested_posts import (
    SUGGESTED_POST_CONNECTOR_KIND,
    suggested_post_external_id,
)


class SuggestedPostAction(str, Enum):
    APPROVE = "approve"
    DECLINE = "decline"


class SuggestedPostActionFailure(str, Enum):
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    MALFORMED_PROVENANCE = "malformed_provenance"
    ROUTING_MISMATCH = "routing_mismatch"
    INSUFFICIENT_RIGHTS = "insufficient_rights"
    NOT_ACTIONABLE = "not_actionable"
    NATIVE_MISSING = "native_missing"
    TRANSIENT_FAILURE = "transient_failure"
    INVALID_REQUEST = "invalid_request"


class SuggestedPostActionError(RuntimeError):
    def __init__(self, failure: SuggestedPostActionFailure, message: str):
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class SuggestedPostStoredIdentity:
    direct_messages_chat_id: int
    message_id: int
    parent_chat_id: int


@dataclass(frozen=True, slots=True)
class SuggestedPostActionResult:
    candidate: ContentCandidate
    document: SourceDocument
    reused_existing: bool


class SuggestedPostActionBot(Protocol):
    async def get_chat(self, chat_id: int): ...

    async def get_me(self): ...

    async def get_chat_member(self, chat_id: int, user_id: int): ...

    async def approve_suggested_post(self, *, chat_id: int, message_id: int) -> bool: ...

    async def decline_suggested_post(
        self,
        *,
        chat_id: int,
        message_id: int,
        comment: str | None = None,
    ) -> bool: ...


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    references: int = 0


_lock_registry_guard = asyncio.Lock()
_lock_registry: dict[int, _LockEntry] = {}


@asynccontextmanager
async def _candidate_action_lock(candidate_id: int) -> AsyncIterator[None]:
    key = int(candidate_id)
    async with _lock_registry_guard:
        entry = _lock_registry.get(key)
        if entry is None:
            entry = _LockEntry(lock=asyncio.Lock())
            _lock_registry[key] = entry
        entry.references += 1

    await entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        async with _lock_registry_guard:
            entry.references -= 1
            if entry.references == 0:
                _lock_registry.pop(key, None)


def _stored_identity(metadata: Mapping[str, Any]) -> SuggestedPostStoredIdentity | None:
    if metadata.get("transport") != "telegram_suggested_posts":
        return None
    dm_chat_id = integer(metadata.get("telegram_direct_messages_chat_id"))
    message_id = integer(metadata.get("telegram_message_id"))
    parent_chat_id = integer(metadata.get("telegram_parent_chat_id"))
    if dm_chat_id in (None, 0) or message_id is None or message_id <= 0:
        return None
    if parent_chat_id in (None, 0):
        return None
    return SuggestedPostStoredIdentity(
        direct_messages_chat_id=dm_chat_id,
        message_id=message_id,
        parent_chat_id=parent_chat_id,
    )


def _target_state(action: SuggestedPostAction) -> str:
    return "approved" if action is SuggestedPostAction.APPROVE else "declined"


def _normalize_comment(action: SuggestedPostAction, comment: str | None) -> str | None:
    normalized = text(comment)
    if action is SuggestedPostAction.APPROVE:
        if normalized is not None:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.INVALID_REQUEST,
                "approve does not accept a decline comment",
            )
        return None
    if normalized is not None and len(normalized) > 128:
        raise SuggestedPostActionError(
            SuggestedPostActionFailure.INVALID_REQUEST,
            "decline comment exceeds Telegram limit",
        )
    return normalized


def _provider_failure(exc: Exception, *, mutation: bool) -> SuggestedPostActionError:
    if isinstance(exc, (TelegramRetryAfter, TelegramNetworkError, TelegramServerError)):
        return SuggestedPostActionError(
            SuggestedPostActionFailure.TRANSIENT_FAILURE,
            "Telegram is temporarily unavailable",
        )
    if isinstance(exc, TelegramNotFound):
        return SuggestedPostActionError(
            SuggestedPostActionFailure.NATIVE_MISSING,
            "native Suggested Post is unavailable",
        )
    if isinstance(exc, TelegramForbiddenError):
        return SuggestedPostActionError(
            SuggestedPostActionFailure.INSUFFICIENT_RIGHTS,
            "Telegram access or administrator rights are unavailable",
        )
    if isinstance(exc, TelegramBadRequest):
        message = str(exc).lower()
        if "not found" in message or "message_id_invalid" in message:
            return SuggestedPostActionError(
                SuggestedPostActionFailure.NATIVE_MISSING,
                "native Suggested Post is unavailable",
            )
        if mutation:
            return SuggestedPostActionError(
                SuggestedPostActionFailure.NOT_ACTIONABLE,
                "Telegram rejected the Suggested Post as no longer actionable",
            )
        return SuggestedPostActionError(
            SuggestedPostActionFailure.NATIVE_MISSING,
            "Telegram chat or member state is unavailable",
        )
    if isinstance(exc, TelegramAPIError):
        return SuggestedPostActionError(
            SuggestedPostActionFailure.TRANSIENT_FAILURE,
            "Telegram request failed",
        )
    return SuggestedPostActionError(
        SuggestedPostActionFailure.TRANSIENT_FAILURE,
        "Telegram request failed",
    )


class SuggestedPostActionService:
    """Own explicit Studio authority for native Suggested Post approve/decline.

    Candidate/source/connector/native identity are always server-derived. A local
    candidate row lock serializes action intent across database workers that honor
    row locks, while the keyed asyncio lock also protects SQLite and same-process
    concurrent requests. Telegram remains lifecycle truth: local terminal metadata
    is written only after a successful native mutation response and later T4.2
    lifecycle updates reconcile into the same stable source identity.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        bot: SuggestedPostActionBot,
    ) -> None:
        self.session = session
        self.bot = bot
        self.sources = SourcesRepo(session)

    async def _load_authority(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
    ) -> tuple[ContentCandidate, SourceDocument, SourceConnector, Channel, SuggestedPostStoredIdentity]:
        candidate = (
            await self.session.execute(
                select(ContentCandidate)
                .where(ContentCandidate.id == int(candidate_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.CANDIDATE_NOT_FOUND,
                "candidate not found",
            )

        document = await self.session.get(SourceDocument, int(candidate.source_document_id))
        if document is None:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.MALFORMED_PROVENANCE,
                "candidate source document is missing",
            )
        connector = await self.session.get(SourceConnector, int(document.connector_id))
        if connector is None:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.MALFORMED_PROVENANCE,
                "candidate source connector is missing",
            )
        channel = await self.session.get(Channel, int(connector.channel_id))
        if channel is None or int(channel.owner_id) != int(actor_client_id):
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.CANDIDATE_NOT_FOUND,
                "candidate not found",
            )

        if not (
            int(candidate.channel_id) == int(document.channel_id) == int(connector.channel_id)
        ):
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.ROUTING_MISMATCH,
                "candidate/source/connector routing disagrees",
            )
        if not connector.enabled or str(connector.kind) != SUGGESTED_POST_CONNECTOR_KIND:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.ROUTING_MISMATCH,
                "Suggested Posts connector is no longer trusted",
            )
        if str(connector.value) != str(int(channel.tg_chat_id)):
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.ROUTING_MISMATCH,
                "connector Telegram channel no longer matches canonical Channel",
            )

        metadata = mapping(document.meta)
        identity = _stored_identity(metadata or {})
        if metadata is None or identity is None:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.MALFORMED_PROVENANCE,
                "stored Suggested Post provenance is incomplete",
            )
        if int(identity.parent_chat_id) != int(channel.tg_chat_id):
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.ROUTING_MISMATCH,
                "stored parent channel disagrees with canonical Channel",
            )
        if str(document.external_id) != suggested_post_external_id(
            identity.direct_messages_chat_id,
            identity.message_id,
        ):
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.MALFORMED_PROVENANCE,
                "stored Suggested Post identity disagrees with source identity",
            )

        matches = [
            row
            for row in await self.sources.list_connectors(int(channel.id))
            if row.enabled
            and str(row.kind) == SUGGESTED_POST_CONNECTOR_KIND
            and str(row.value) == str(int(channel.tg_chat_id))
        ]
        if len(matches) != 1 or int(matches[0].id) != int(connector.id):
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.ROUTING_MISMATCH,
                "trusted Suggested Posts connector mapping is no longer unique",
            )
        return candidate, document, connector, channel, identity

    async def _fresh_parent_check(
        self,
        *,
        channel: Channel,
        identity: SuggestedPostStoredIdentity,
    ) -> None:
        try:
            chat = await self.bot.get_chat(int(identity.direct_messages_chat_id))
        except Exception as exc:
            raise _provider_failure(exc, mutation=False) from exc

        if getattr(chat, "is_direct_messages", None) is not True:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.ROUTING_MISMATCH,
                "Telegram chat is no longer a channel direct-messages chat",
            )
        parent_chat = getattr(chat, "parent_chat", None)
        parent_chat_id = integer(getattr(parent_chat, "id", None))
        if parent_chat_id is None or int(parent_chat_id) != int(channel.tg_chat_id):
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.ROUTING_MISMATCH,
                "Telegram direct-messages parent no longer matches canonical Channel",
            )

    async def _fresh_rights_check(
        self,
        *,
        action: SuggestedPostAction,
        channel: Channel,
    ) -> None:
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(
                int(channel.tg_chat_id),
                int(me.id),
            )
        except Exception as exc:
            raise _provider_failure(exc, mutation=False) from exc

        required = (
            "can_post_messages"
            if action is SuggestedPostAction.APPROVE
            else "can_manage_direct_messages"
        )
        if getattr(member, required, None) is not True:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.INSUFFICIENT_RIGHTS,
                f"Telegram administrator right {required} is required",
            )

    async def _mutate_native(
        self,
        *,
        action: SuggestedPostAction,
        identity: SuggestedPostStoredIdentity,
        comment: str | None,
    ) -> None:
        try:
            if action is SuggestedPostAction.APPROVE:
                # Price and send_date are deliberately omitted. Telegram's stored
                # Suggested Post terms remain the only native business authority.
                success = await self.bot.approve_suggested_post(
                    chat_id=int(identity.direct_messages_chat_id),
                    message_id=int(identity.message_id),
                )
            else:
                success = await self.bot.decline_suggested_post(
                    chat_id=int(identity.direct_messages_chat_id),
                    message_id=int(identity.message_id),
                    comment=comment,
                )
        except Exception as exc:
            raise _provider_failure(exc, mutation=True) from exc
        if success is not True:
            raise SuggestedPostActionError(
                SuggestedPostActionFailure.TRANSIENT_FAILURE,
                "Telegram did not confirm Suggested Post mutation",
            )

    async def _reconcile_native_success(
        self,
        *,
        action: SuggestedPostAction,
        connector: SourceConnector,
        identity: SuggestedPostStoredIdentity,
        comment: str | None,
    ) -> SuggestedPostActionResult:
        event = _target_state(action)
        payload: dict[str, Any] = {}
        if action is SuggestedPostAction.DECLINE and comment is not None:
            payload["comment"] = comment
        lifecycle = {
            "event": event,
            "origin": "studio_native_action_result",
            "payload": payload,
        }
        result = await SourceIngestionReconciliationService(self.session).reconcile(
            connector,
            SourceProjection(
                external_id=suggested_post_external_id(
                    identity.direct_messages_chat_id,
                    identity.message_id,
                ),
                metadata={
                    "transport": "telegram_suggested_posts",
                    "telegram_direct_messages_chat_id": identity.direct_messages_chat_id,
                    "telegram_message_id": identity.message_id,
                    "telegram_parent_chat_id": identity.parent_chat_id,
                    "telegram_suggested_post_lifecycle": lifecycle,
                    f"telegram_suggested_post_{event}": lifecycle,
                },
                update_mode=SourceProjectionUpdateMode.LIFECYCLE,
            ),
        )
        return SuggestedPostActionResult(
            candidate=result.candidate,
            document=result.document,
            reused_existing=False,
        )

    async def execute(
        self,
        *,
        candidate_id: int,
        actor_client_id: int,
        action: SuggestedPostAction,
        comment: str | None = None,
    ) -> SuggestedPostActionResult:
        normalized_comment = _normalize_comment(action, comment)
        async with _candidate_action_lock(candidate_id):
            try:
                candidate, document, connector, channel, identity = await self._load_authority(
                    candidate_id=candidate_id,
                    actor_client_id=actor_client_id,
                )
                metadata = mapping(document.meta) or {}
                current_state = effective_native_state(metadata)
                target_state = _target_state(action)

                if current_state == target_state:
                    await self.session.commit()
                    return SuggestedPostActionResult(
                        candidate=candidate,
                        document=document,
                        reused_existing=True,
                    )
                if current_state != "pending":
                    raise SuggestedPostActionError(
                        SuggestedPostActionFailure.NOT_ACTIONABLE,
                        f"stored Suggested Post state {current_state!r} is not actionable",
                    )
                if (
                    action is SuggestedPostAction.APPROVE
                    and not paid_proposal_is_safe_to_approve(metadata)
                ):
                    raise SuggestedPostActionError(
                        SuggestedPostActionFailure.INVALID_REQUEST,
                        "stored Suggested Post price is not understood",
                    )

                await self._fresh_parent_check(channel=channel, identity=identity)
                await self._fresh_rights_check(action=action, channel=channel)
                await self._mutate_native(
                    action=action,
                    identity=identity,
                    comment=normalized_comment,
                )
                return await self._reconcile_native_success(
                    action=action,
                    connector=connector,
                    identity=identity,
                    comment=normalized_comment,
                )
            except Exception:
                await self.session.rollback()
                raise
