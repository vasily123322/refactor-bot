from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.canonical_publication_result_link import (
    CanonicalPublicationResultLinkResolver,
)
from app.services.publication_bridge import LegacyPublicationBridge


class _LinkBot:
    def __init__(self, *, username: str | None = None, error: Exception | None = None) -> None:
        self.username = username
        self.error = error
        self.calls: list[int] = []

    async def get_chat(self, chat_id: int):
        self.calls.append(int(chat_id))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(username=self.username)


class _Sender:
    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        return [1701, 1702]


class _CancellingResolver:
    async def resolve(self, *, chat_id: int, message_ids):
        raise asyncio.CancelledError()


async def _seed(Session, *, seed: int, tg_chat_id: int) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=131000 + seed,
            username=f"result-link-{seed}",
            full_name=f"Result Link {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=tg_chat_id,
            title=f"Result Link {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Canonical result-link proof"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        publication.legacy_post_task_id = None
        if task is not None:
            await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_private_result_link_is_deterministic_without_provider_lookup() -> None:
    async def run() -> None:
        bot = _LinkBot(username="ignored")
        link = await CanonicalPublicationResultLinkResolver(bot).resolve(
            chat_id=-100777,
            message_ids=[41, 42],
        )
        assert link == "https://t.me/c/777/42"
        assert bot.calls == []
    asyncio.run(run())


def test_public_result_link_uses_only_safe_provider_username() -> None:
    async def run() -> None:
        bot = _LinkBot(username="public_channel")
        assert await CanonicalPublicationResultLinkResolver(bot).resolve(
            chat_id=-777, message_ids=[42]
        ) == "https://t.me/public_channel/42"
        unsafe = _LinkBot(username='bad"><b>inject</b>')
        assert await CanonicalPublicationResultLinkResolver(unsafe).resolve(
            chat_id=-778, message_ids=[43]
        ) is None
    asyncio.run(run())


def test_public_lookup_failure_is_best_effort_without_raw_provider_log() -> None:
    async def run() -> None:
        bot = _LinkBot(error=RuntimeError("https://api.telegram.org/botSUPERSECRET/getChat failed"))
        messages: list[str] = []
        sink_id = logger.add(messages.append, format="{message}")
        try:
            link = await CanonicalPublicationResultLinkResolver(bot).resolve(
                chat_id=-779, message_ids=[44]
            )
        finally:
            logger.remove(sink_id)
        assert link is None
        rendered = "\n".join(messages)
        assert "SUPERSECRET" not in rendered
        assert "error_type=RuntimeError" in rendered
    asyncio.run(run())


def test_executor_persists_enriched_link_with_primary_success(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'canonical-result-link.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(Session, seed=1, tg_chat_id=-100888)
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_Sender(),
                result_link_resolver=CanonicalPublicationResultLinkResolver(_LinkBot()),
            ).execute(publication_id)
            assert result.outcome == "published"
            assert result.result_link == "https://t.me/c/888/1702"
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.result_link == "https://t.me/c/888/1702"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_cancelled_enrichment_leaves_ambiguous_claim_for_recovery(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'canonical-result-link-cancel.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(Session, seed=2, tg_chat_id=-100889)
            executor = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_Sender(),
                result_link_resolver=_CancellingResolver(),  # type: ignore[arg-type]
            )
            with pytest.raises(asyncio.CancelledError):
                await executor.execute(publication_id)
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.result_link is None
                assert await session.get(PublicationDeliveryLease, publication_id) is not None
        finally:
            await engine.dispose()
    asyncio.run(run())
