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


class TelegramSourceGateway(Protocol):
    async def get_chat(self, target: str | int) -> UserbotChat: ...

    async def join_chat(self, target: str | int): ...

    def get_chat_history(self, target: str | int, *, limit: int = 100): ...


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
    """Project Telegram channel history into the normalized Sources v2 domain."""

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

    async def ingest(self, connector: SourceConnector) -> IngestionResult:
        if str(connector.kind).lower() != "telegram":
            raise SourceIngestionError("Telegram ingestion requires a telegram connector")
        if not connector.enabled:
            return IngestionResult(int(connector.id), 0, 0, 0)

        chat = await self._resolve_chat(connector)
        seen = 0
        created_count = 0
        candidate_count = 0
        latest_published_at: datetime | None = None
        try:
            # Read by the resolved peer id. Private invite URLs are valid join targets
            # but are not reliable history lookup identifiers after the join completes.
            async for message in self.gateway.get_chat_history(
                int(chat.id),
                limit=self.history_limit,
            ):
                text = _message_text(message)
                if not text:
                    # The current normalized document contract is textual. Media-only
                    # posts are intentionally deferred to the media attachment adapter.
                    continue
                seen += 1
                published_at = _normalize_message_date(message.date)
                if published_at is not None and (
                    latest_published_at is None or published_at > latest_published_at
                ):
                    latest_published_at = published_at
                document, created = await self.repo.upsert_document(
                    connector=connector,
                    external_id=f"telegram:{int(chat.id)}:{int(message.id)}",
                    content=text,
                    source_url=_message_url(chat, int(message.id)),
                    title=chat.title,
                    published_at=published_at,
                    metadata={
                        "connector_kind": "telegram",
                        "telegram_chat_id": int(chat.id),
                        "telegram_message_id": int(message.id),
                        "citation_enabled": bool(connector.citation_enabled),
                        "reuse_policy": str(connector.reuse_policy),
                    },
                )
                if created:
                    created_count += 1
                    await self.repo.ensure_candidate(
                        source_document_id=document.id,
                        channel_id=connector.channel_id,
                        suggested_action=_candidate_action(connector),
                        metadata={
                            "source_connector_id": int(connector.id),
                            "reuse_policy": str(connector.reuse_policy),
                        },
                    )
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
