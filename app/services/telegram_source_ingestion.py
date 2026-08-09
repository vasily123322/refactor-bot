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
from app.userbot.client import UserbotChat, UserbotMessage, app as userbot


TELEGRAM_CURSOR_KEY = "telegram_cursor_message_id"


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


def _normalize_message_date(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _candidate_action(connector: SourceConnector) -> str:
    mode = str(connector.mode or "research")
    if mode == "summary":
        return "summarize"
    if mode == "rewrite":
        return "rewrite"
    if mode == "mirror":
        return "mirror" if connector.reuse_policy == "mirror_authorized" else "review"
    return "research"


def _message_text(message: UserbotMessage) -> str:
    value = (message.text or message.caption or "").strip()
    if len(value) <= MAX_SOURCE_DOCUMENT_CHARS:
        return value
    return value[:MAX_SOURCE_DOCUMENT_CHARS].rstrip() + "…"


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

    async def _resolve_chat(self, connector: SourceConnector) -> UserbotChat:
        target = str(connector.value).strip()
        try:
            return await self.gateway.get_chat(target)
        except Exception:
            # Private invite links and some channels require joining first. This
            # matches the existing inline source-access semantics.
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

    async def _stage_cursor(
        self,
        connector: SourceConnector,
        highest_message_id: int,
    ) -> None:
        if highest_message_id <= 0:
            return
        # Another manual/worker ingestion may have advanced the same connector while
        # this network read was in flight. Refresh only config and never move the
        # cursor backwards.
        await self.session.refresh(connector, attribute_names=["config"])
        persisted = telegram_cursor_message_id(connector)
        if highest_message_id <= persisted:
            return
        connector.config = {
            **dict(connector.config or {}),
            TELEGRAM_CURSOR_KEY: int(highest_message_id),
        }

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
            # Bootstrap intentionally keeps the historical "latest N" behavior. Once
            # a cursor exists, read oldest->newest above min_id so a backlog larger
            # than one page is drained over multiple ticks without skipping rows.
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

                text = _message_text(message)
                if not text:
                    # Media-only posts are intentionally deferred to the media
                    # attachment adapter, but their IDs still advance the cursor so
                    # they are not re-read forever.
                    continue
                seen += 1
                published_at = _normalize_message_date(message.date)
                if published_at is not None and (
                    latest_published_at is None or published_at > latest_published_at
                ):
                    latest_published_at = published_at
                document, created = await self.repo.upsert_document(
                    connector=connector,
                    external_id=f"telegram:{int(chat.id)}:{message_id}",
                    content=text,
                    source_url=_message_url(chat, message_id),
                    title=chat.title,
                    published_at=published_at,
                    metadata={
                        "connector_kind": "telegram",
                        "telegram_chat_id": int(chat.id),
                        "telegram_message_id": message_id,
                        "citation_enabled": bool(connector.citation_enabled),
                        "reuse_policy": str(connector.reuse_policy),
                    },
                )
                if created:
                    created_count += 1
                # Always ensure the candidate. If document persistence succeeded but
                # a previous run failed before candidate creation, the retry heals
                # that partial commit instead of leaving an orphan document forever.
                await self.repo.ensure_candidate(
                    source_document_id=document.id,
                    channel_id=connector.channel_id,
                    suggested_action=_candidate_action(connector),
                    metadata={
                        "source_connector_id": int(connector.id),
                        "reuse_policy": str(connector.reuse_policy),
                    },
                )
                if created:
                    candidate_count += 1
        except SourceIngestionError:
            raise
        except Exception as exc:
            # Cursor is staged only after the iterator finishes successfully. Any
            # documents committed before this failure are safe to replay because
            # external_id/candidate constraints make the retry idempotent.
            await self.repo.update_health(
                connector,
                status="broken",
                reason=f"Telegram history read failed: {type(exc).__name__}",
                auth_state="session_required",
                success=False,
            )
            raise SourceIngestionError("Telegram history read failed") from exc

        if fetched == 0 and cursor > 0:
            # Being caught up is a healthy no-op, not a degraded source. Avoid a DB
            # write on every worker tick unless this poll actually recovers state.
            if connector.status != "healthy" or connector.auth_state != "ready":
                await self.repo.update_health(
                    connector,
                    status="healthy",
                    reason=None,
                    auth_state="ready",
                    success=True,
                )
            return IngestionResult(int(connector.id), 0, 0, 0)

        await self._stage_cursor(connector, highest_message_id)
        if seen == 0:
            await self.repo.update_health(
                connector,
                status="degraded",
                reason="Telegram source returned no text messages",
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
