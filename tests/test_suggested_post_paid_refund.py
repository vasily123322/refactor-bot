from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.studio.suggested_post_read_model import project_suggested_post_inbox
from app.core.db import Base
from app.domain.content.models import ContentItem
from app.domain.models import Channel
from app.domain.publishing.models import Publication
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.telegram_suggested_posts import (
    SUGGESTED_POST_CONNECTOR_KIND,
    TelegramSuggestedPostDisposition,
    TelegramSuggestedPostIngestionService,
)


DM_CHAT_ID = -1009001
PARENT_CHAT_ID = -1007001
USER = {"id": 501, "is_bot": False, "first_name": "Alice", "username": "alice"}


class FakeBot:
    async def get_chat(self, chat_id: int):
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=PARENT_CHAT_ID),
        )


def _proposal() -> Message:
    return Message.model_validate(
        {
            "message_id": 77,
            "date": datetime(2026, 8, 18, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private"},
            "from": USER,
            "text": "Paid proposal",
            "direct_messages_topic": {"topic_id": 123, "user": USER},
            "suggested_post_info": {
                "state": "approved",
                "price": {"currency": "XTR", "amount": 120},
            },
        }
    )


def _paid(original: Message, *, message_id: int = 910) -> Message:
    return Message.model_validate(
        {
            "message_id": message_id,
            "date": datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private"},
            "suggested_post_paid": {
                "suggested_post_message": original.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
                "currency": "XTR",
                "star_amount": {"amount": 120, "nanostar_amount": 5},
            },
        }
    )


def _refunded(
    original: Message,
    *,
    message_id: int = 920,
    reason: str = "post_deleted",
) -> Message:
    return Message.model_validate(
        {
            "message_id": message_id,
            "date": datetime(2026, 8, 18, 4, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private"},
            "suggested_post_refunded": {
                "suggested_post_message": original.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
                "reason": reason,
            },
        }
    )


async def _seed(session):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical")
    session.add(channel)
    await session.flush()
    connector = await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=SUGGESTED_POST_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    service = TelegramSuggestedPostIngestionService(session, bot=FakeBot())
    initial = await service.ingest(_proposal())
    assert initial.reconciliation is not None
    candidate = initial.reconciliation.candidate
    candidate.meta = {
        **dict(candidate.meta or {}),
        "rewrite_run_id": 4242,
        "rewrite_provider": "channel_ai_structured",
    }
    await session.commit()
    return service, connector, initial.reconciliation.document, candidate


def test_paid_and_refund_events_reconcile_same_source_candidate_without_product_authority() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                service, connector, document, candidate = await _seed(session)
                document_id = int(document.id)
                candidate_id = int(candidate.id)
                original = _proposal()

                first_paid = await service.ingest(_paid(original))
                duplicate_paid = await service.ingest(_paid(original))
                refund = await service.ingest(_refunded(original))
                duplicate_refund = await service.ingest(_refunded(original))

                for result in (first_paid, duplicate_paid, refund, duplicate_refund):
                    assert result.disposition is TelegramSuggestedPostDisposition.LIFECYCLE
                    assert result.reconciliation is not None
                    assert int(result.reconciliation.document.id) == document_id
                    assert int(result.reconciliation.candidate.id) == candidate_id
                    assert int(result.reconciliation.document.connector_id) == int(connector.id)

                assert await session.scalar(select(func.count()).select_from(SourceDocument)) == 1
                assert await session.scalar(select(func.count()).select_from(ContentCandidate)) == 1
                assert await session.scalar(select(func.count()).select_from(ContentItem)) == 0
                assert await session.scalar(select(func.count()).select_from(Publication)) == 0

                persisted = await session.get(SourceDocument, document_id)
                current_candidate = await session.get(ContentCandidate, candidate_id)
                assert persisted is not None and current_candidate is not None
                assert persisted.meta["telegram_suggested_post_paid"]["service_message_id"] == 910
                assert persisted.meta["telegram_suggested_post_paid"]["service_message_date"].startswith(
                    "2026-08-18T02:00:00"
                )
                assert persisted.meta["telegram_suggested_post_refunded"]["payload"]["reason"] == "post_deleted"
                assert current_candidate.status == "new"
                assert current_candidate.content_item_id is None
                assert current_candidate.meta["rewrite_run_id"] == 4242
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_out_of_order_provider_delivery_keeps_newer_refund_as_effective_business_state() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                service, _, document, candidate = await _seed(session)
                original = _proposal()
                await service.ingest(_refunded(original, message_id=920, reason="payment_refunded"))
                # Delayed older provider update arrives last and overwrites the compatibility current key.
                await service.ingest(_paid(original, message_id=910))

                persisted = await session.get(SourceDocument, int(document.id))
                assert persisted is not None
                assert persisted.meta["telegram_suggested_post_lifecycle"]["event"] == "paid"
                view = project_suggested_post_inbox(dict(persisted.meta or {}))
                assert view is not None
                assert view.native_status == "refunded"
                assert view.refund_reason_code == "payment_refunded"
                assert view.refunded_service_message_id == 920
                current_candidate = await session.get(ContentCandidate, int(candidate.id))
                assert current_candidate is not None
                assert current_candidate.meta["rewrite_run_id"] == 4242
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_paid_suggested_post_projection_never_materializes_canonical_destructive_target() -> None:
    """Paid native Suggested Post identity remains source provenance, not Publication ids.

    Canonical edit/autodelete services require a published Publication and its own
    delivery-produced telegram_message_ids. T4.2/T4.5 never create that linkage, so the
    provider-owned paid channel post cannot enter those destructive paths by amount,
    sender, time, or the DM source message id.
    """

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                service, _, document, candidate = await _seed(session)
                await service.ingest(_paid(_proposal()))
                assert await session.scalar(select(func.count()).select_from(Publication)) == 0
                assert await session.scalar(select(func.count()).select_from(ContentItem)) == 0
                current = await session.get(ContentCandidate, int(candidate.id))
                source = await session.get(SourceDocument, int(document.id))
                assert current is not None and source is not None
                assert current.content_item_id is None
                assert source.external_id == f"dm:{DM_CHAT_ID}:77"
        finally:
            await engine.dispose()

    asyncio.run(run())
