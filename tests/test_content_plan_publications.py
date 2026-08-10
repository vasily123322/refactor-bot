from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.repositories.content import ContentRepo
from app.services.content_plan_publications import load_owned_publication_context
from app.services.publication_bridge import LegacyPublicationBridge


def test_owned_publication_context_survives_post_task_retirement(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'cp-publication.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                owner = Client(tg_user_id=7001, username="owner", full_name="Owner")
                stranger = Client(tg_user_id=7002, username="stranger", full_name="Stranger")
                session.add_all([owner, stranger])
                await session.flush()
                channel = Channel(
                    owner_id=int(owner.id),
                    tg_chat_id=-1007001001,
                    title="Owned channel",
                )
                session.add(channel)
                await session.commit()
                await session.refresh(channel)

                item = await ContentRepo(session).create(
                    channel_id=int(channel.id),
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Canonical edit"}]
                    ),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    runtime_options={"autodelete_seconds": 3600},
                )
                publication_id = int(publication.id)
                task_id = int(publication.legacy_post_task_id or 0)
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [88001],
                    "result_link": "https://t.me/example/88001",
                }
                await session.commit()
                publication = await LegacyPublicationBridge(session).reconcile(publication_id)
                assert publication.status == "published"

                # Simulate the future published-transport retention boundary. Do this
                # explicitly because this SQLite test intentionally does not enable FK
                # enforcement and must not rely on ON DELETE SET NULL.
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

            async with Session() as session:
                context = await load_owned_publication_context(
                    session,
                    publication_id=publication_id,
                    tg_user_id=7001,
                )
                assert context is not None
                assert context.status == "published"
                assert context.channel_id == int(channel.id)
                assert context.telegram_message_ids == (88001,)
                assert context.result_link == "https://t.me/example/88001"
                assert context.legacy_post_task_id is None
                assert context.editor_payload["type"] == "text"
                assert context.editor_payload["text"] == "Canonical edit"
                assert context.editor_payload["autodelete_seconds"] == 3600

                denied = await load_owned_publication_context(
                    session,
                    publication_id=publication_id,
                    tg_user_id=7002,
                )
                assert denied is None
        finally:
            await engine.dispose()

    asyncio.run(run())
