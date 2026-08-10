from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_editor import load_owned_publication_editor_view


def test_publication_editor_rejects_cross_channel_content_linkage(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-editor-cross-channel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                owner = Client(
                    tg_user_id=71001,
                    username="owner",
                    full_name="Owner",
                    ui_settings={},
                )
                foreign_owner = Client(
                    tg_user_id=71002,
                    username="foreign",
                    full_name="Foreign",
                    ui_settings={},
                )
                session.add_all([owner, foreign_owner])
                await session.flush()
                owned_channel = Channel(
                    tg_chat_id=-10071001,
                    title="Owned",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                foreign_channel = Channel(
                    tg_chat_id=-10071002,
                    title="Foreign",
                    owner_id=int(foreign_owner.id),
                    is_active=True,
                )
                session.add_all([owned_channel, foreign_channel])
                await session.commit()

                owned_item = await ContentRepo(session).create(
                    channel_id=int(owned_channel.id),
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Owned"}]
                    ),
                )
                foreign_item = await ContentRepo(session).create(
                    channel_id=int(foreign_channel.id),
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Foreign secret"}]
                    ),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(owned_item.id)
                )
                publication_id = int(publication.id)
                schedule_id = int(publication.schedule_entry_id or 0)

                # Corrupt both canonical pointers so Publication/Schedule agree with
                # each other but point at a ContentItem owned by another tenant.
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert publication is not None and schedule is not None
                publication.content_item_id = int(foreign_item.id)
                publication.content_revision = int(foreign_item.current_revision)
                schedule.content_item_id = int(foreign_item.id)
                schedule.content_revision = int(foreign_item.current_revision)
                await session.commit()

            async with Session() as session:
                view = await load_owned_publication_editor_view(
                    session,
                    publication_id=publication_id,
                    tg_user_id=71001,
                )
                assert view is None
        finally:
            await engine.dispose()

    asyncio.run(run())
