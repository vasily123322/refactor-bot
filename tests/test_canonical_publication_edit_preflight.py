from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client
from app.domain.publishing.models import ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_edit import CanonicalPublicationEditCoordinator
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_edit_persistence import PublicationEditConflictError


class FakeProvider:
    def __init__(self) -> None:
        self.text_calls: list[int] = []

    async def edit_message_text(self, **kwargs):
        self.text_calls.append(int(kwargs["message_id"]))
        return object()

    async def edit_message_media(self, **kwargs):
        raise AssertionError("media edit not expected")


def test_inconsistent_schedule_lifecycle_blocks_provider_before_side_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit-lifecycle-preflight.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                owner = Client(
                    tg_user_id=73101,
                    username="owner",
                    full_name="Owner",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-10073101,
                    title="Lifecycle guard",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                session.add(channel)
                await session.commit()

                item = await ContentRepo(session).create(
                    channel_id=int(channel.id),
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Before"}]
                    ),
                    created_by_tg_user_id=73101,
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id)
                )
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                publication.status = "published"
                publication.telegram_message_ids = [93101]
                # Deliberately leave ScheduleEntry non-completed while Publication says
                # published. Provider must not be called for this inconsistent state.
                assert schedule.status != "completed"
                await session.commit()
                publication_id = int(publication.id)

            provider = FakeProvider()
            with pytest.raises(PublicationEditConflictError):
                await CanonicalPublicationEditCoordinator(
                    provider=provider,
                    session_factory=Session,
                ).edit_text_and_persist(
                    publication_id=publication_id,
                    tg_user_id=73101,
                    expected_revision=1,
                    payload={"type": "text", "text": "Should not be sent"},
                    text="Should not be sent",
                )
            assert provider.text_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
