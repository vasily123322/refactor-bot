from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.models import SourceConnector
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion import (
    MAX_SOURCE_DOCUMENT_CHARS,
    IngestionResult,
    SourceIngestionError,
)
from app.services.source_reconciliation import (
    SourceIngestionReconciliationService,
    SourceProjection,
)
from app.userbot.client import UserbotChat, UserbotMessage, app as userbot


TELEGRAM_CURSOR_KEY = "telegram_cursor_message_id"
TELEGRAM_BACKLOG_HINT_KEY = "telegram_backlog_hint"


class TelegramSourceGateway(Protocol):
    async def get_chat(self, target: str | int) -> UserbotChat: ...

    async def join_chat(self, target: str | int): ...

    def get_chat_history(
        self,
        target: str | int,
        *,
        limit: int = 100,
        min_id: int = 0,
        reverse: bool = False,
    ): ...


def telegram_cursor_message_id(connector: SourceConnector) -> int:
    raw = dict(connector.config or {}).get(TELEGRAM_CURSOR_KEY)
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def telegram_backlog_hint(connector: SourceConnector) -> bool:
    return bool(dict(connector.config or {}).get(TELEGRAM_BACKLOG_HINT_KEY, False))


def _normalize_message_date(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _message_text(message: UserbotMessage) -> str:
    value = (message.text or message.caption or "").strip()
    if len(value) <= MAX_SOURCE_DOCUMENT_CHARS:
        return value
    return value[:MAX_SOURCE_DOCUMENT_CHARS].rstrip() + "…"


def _message_content(message: UserbotMessage) -> str:
    text = _message_text(message)
    if text:
        return text
    if message.media is not None:
        # Keep SourceDocument.content non-empty so media-only messages survive the
        # normalized Sources/Inbox pipeline. Transport/session identifiers are not
        # included; the safe descriptor lives separately in metadata.
        return f"[Telegram {message.media.kind}]"
    return ""


def _message_url(chat: UserbotChat, message_id: int) -> str | None:
    username = (chat.username or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}/{int(message_id)}"


class TelegramSourceIngestionService:
    """Project Telegram channel history into the normalized Sources v2 domain.

    The first successful read bootstraps from the newest bounded history window.
    Afterwards the persisted connector cursor reads the oldest messages strictly
    newer than that cursor, so a backlog larger than one page is drained without
    gaps over successive worker ticks.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        gateway: TelegramSourceGateway = userbot,
        history_limit: int = 100,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.history_limit = max(1, min(int(history_limit), 500))
        self.repo = SourcesRepo(session)
        self.reconciler = SourceIngestionReconciliationService(session)

    async def _resolve_chat(self, connector: SourceConnector) -> UserbotChat:
        target = str(connector.value).strip()
        try:
            return await self.gateway.get_chat(target)
        except Exception:
            try:
                await self.gateway.join_chat(target)
                return await self.gateway.get_chat(target)
            except Exception as exc:
                await self.repo.update_health(
                    connector,
                    status="auth_required",
                    reason="Telegram source is not accessible to the userbot session",
                    auth_state="session_required",
                    success=False,
                )
                raise SourceIngestionError("Telegram source is not accessible") from exc

    async def _stage_runtime_state(
        self,
        connector: SourceConnector,
        *,
        highest_message_id: int,
        backlog_hint: bool,
    ) -> tuple[int, bool]:
        """Persist monotonic cursor + backlog hint without clobbering a newer poll."""
        await self.session.refresh(connector, attribute_names=["config"])
        persisted_cursor = telegram_cursor_message_id(connector)
        if int(highest_message_id) < persisted_cursor:
            return persisted_cursor, telegram_backlog_hint(connector)

        next_cursor = max(persisted_cursor, int(highest_message_id))
        next_hint = bool(backlog_hint)
        current = dict(connector.config or {})
        if (
            int(current.get(TELEGRAM_CURSOR_KEY) or 0) == next_cursor
            and bool(current.get(TELEGRAM_BACKLOG_HINT_KEY, False)) == next_hint
        ):
            return next_cursor, next_hint
        connector.config = {
            **current,
            TELEGRAM_CURSOR_KEY: next_cursor,
            TELEGRAM_BACKLOG_HINT_KEY: next_hint,
        }
        return next_cursor, next_hint

    async def ingest(self, connector: SourceConnector) -> IngestionResult:
        if str(connector.kind).lower() != "telegram":
            raise SourceIngestionError("Telegram ingestion requires a telegram connector")
        if not connector.enabled:
            return IngestionResult(int(connector.id), 0, 0, 0)

        chat = await self._resolve_chat(connector)
        cursor = telegram_cursor_message_id(connector)
        seen = 0
        fetched = 0
        created_count = 0
        candidate_count = 0
        highest_message_id = cursor
        latest_published_at: datetime | None = None
        try:
            async for message in self.gateway.get_chat_history(
                int(chat.id),
                limit=self.history_limit,
                min_id=cursor,
                reverse=bool(cursor),
            ):
                message_id = int(message.id or 0)
                if message_id <= 0:
                    continue
                fetched += 1
                highest_message_id = max(highest_message_id, message_id)

                content = _message_content(message)
                if not content:
                    continue
                seen += 1
                published_at = _normalize_message_date(message.date)
                if published_at is not None and (
                    latest_published_at is None or published_at > latest_published_at
                ):
                    latest_published_at = published_at
                metadata: dict[str, object] = {
                    "connector_kind": "telegram",
                    "telegram_chat_id": int(chat.id),
                    "telegram_message_id": message_id,
                    "citation_enabled": bool(connector.citation_enabled),
                    "reuse_policy": str(connector.reuse_policy),
                }
                if message.media is not None:
                    metadata["telegram_media"] = message.media.to_metadata()
                result = await self.reconciler.reconcile(
                    connector,
                    SourceProjection(
                        external_id=f"telegram:{int(chat.id)}:{message_id}",
                        content=content,
                        source_url=_message_url(chat, message_id),
                        title=chat.title,
                        published_at=published_at,
                        metadata=metadata,
                    ),
                )
                if result.document_created:
                    created_count += 1
                if result.candidate_created:
                    candidate_count += 1
        except SourceIngestionError:
            raise
        except Exception as exc:
            await self.repo.update_health(
                connector,
                status="broken",
                reason=f"Telegram history read failed: {type(exc).__name__}",
                auth_state="session_required",
                success=False,
            )
            raise SourceIngestionError("Telegram history read failed") from exc

        if fetched == 0 and cursor > 0:
            _, hint = await self._stage_runtime_state(
                connector,
                highest_message_id=cursor,
                backlog_hint=False,
            )
            needs_health_recovery = connector.status != "healthy" or connector.auth_state != "ready"
            if needs_health_recovery:
                await self.repo.update_health(
                    connector,
                    status="healthy",
                    reason=None,
                    auth_state="ready",
                    success=True,
                )
            elif hint is False and self.session.is_modified(connector, include_collections=False):
                await self.session.commit()
            return IngestionResult(int(connector.id), 0, 0, 0)

        # A full incremental page is a conservative hint that more messages may
        # remain. Bootstrap pages intentionally do not set it because older history
        # is outside the incremental contract.
        backlog_hint = bool(cursor > 0 and fetched >= self.history_limit)
        await self._stage_runtime_state(
            connector,
            highest_message_id=highest_message_id,
            backlog_hint=backlog_hint,
        )
        if seen == 0:
            await self.repo.update_health(
                connector,
                status="degraded",
                reason="Telegram source returned no ingestible messages",
                auth_state="ready",
                success=False,
            )
        else:
            connector.auth_state = "ready"
            if latest_published_at is not None:
                connector.last_document_at = latest_published_at
            await self.session.commit()

        return IngestionResult(
            connector_id=int(connector.id),
            documents_seen=seen,
            documents_created=created_count,
            candidates_created=candidate_count,
        )
