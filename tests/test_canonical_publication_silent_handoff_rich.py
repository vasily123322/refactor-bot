from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_legacy_transport_handoff import (
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_rich(
    Session,
    *,
    seed: int,
    document_silent: bool,
    runtime_silent: bool,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=184000 + seed,
            username=f"silent-rich-handoff-{seed}",
            full_name=f"Silent Rich Handoff {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(184100 + seed),
            title=f"Silent Rich Handoff {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                mode="rich",
                blocks=[
                    {
                        "id": "b1",
                        "type": "paragraph",
                        "content": f"Rich silent handoff {seed}",
                    }
                ],
                telegram={"silent": document_silent},
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={"silent": runtime_silent},
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_rich_handoff_rejects_top_level_silent_when_legacy_effective_value_differs(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-rich-handoff-mismatch.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_rich(
                Session,
                seed=1,
                document_silent=False,
                runtime_silent=True,
            )

            async with Session() as before_session:
                task = await before_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                assert payload.get("silent") is True
                assert payload["post_document"]["telegram"]["silent"] is False

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(publication_id)
                assert result.outcome == "ineligible"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rich_handoff_accepts_explicit_silent_only_when_legacy_effective_value_matches(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-rich-handoff-match.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_rich(
                Session,
                seed=2,
                document_silent=True,
                runtime_silent=True,
            )

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(publication_id)
                assert result.outcome == "retired"

            async with Session() as check_session:
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id is None
                assert await check_session.get(PostTask, task_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
